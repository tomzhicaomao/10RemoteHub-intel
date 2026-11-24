"""Opt-in continuous capture on any WinUSB-bound USB Bluetooth HCI adapter."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import sys

from .ble_dongle_reader import parse_adapter_spec
from .ble_remote import BLERemoteReports, VOICE_COMMAND_REPORT, VOICE_TAIL_SECONDS


_KEY_REPORTS = ((1, 1), (3, 1), (4, 1))
_VOICE_REPORTS = ((0xF8, 1), (0xFC, 1))
_TIMEOUT = 8
_MAX_VOICE_SECONDS = 60


async def _characteristics(peer, service_id, uuid):
    services = await peer.discover_service(uuid(service_id))
    if len(services) != 1:
        raise ValueError(f"遥控器的 GATT 服务 {service_id:04X} 缺失或重复")
    service = services[0]
    await service.discover_characteristics()
    return service.characteristics


def _one(items, uid):
    matches = [item for item in items if item.uuid == uid]
    if len(matches) != 1:
        raise ValueError("遥控器的 GATT 字段缺失或重复")
    return matches[0]


async def _discover(peer, uuid, microphone):
    info = await _characteristics(peer, 0x180A, uuid)
    software = await _one(info, uuid(0x2A28)).read_value()
    hid = await _characteristics(peer, 0x1812, uuid)
    report_map = await _one(hid, uuid(0x2A4B)).read_value()
    stream = BLERemoteReports(software.rstrip(b"\x00").decode("ascii"), report_map, use_remote_mic=microphone)

    wanted = set(_KEY_REPORTS)
    if microphone:
        wanted.update((*_VOICE_REPORTS, (VOICE_COMMAND_REPORT, 2)))
    reports = {}
    for item in hid:
        if item.uuid != uuid(0x2A4D):
            continue
        for descriptor in await item.discover_descriptors():
            if descriptor.type != uuid(0x2908):
                continue
            reference = tuple(await descriptor.read_value())
            if reference in wanted:
                if reference in reports:
                    raise ValueError("遥控器重复声明同一种 BLE 报告")
                reports[reference] = item
    required = {(1, 1)}
    if microphone:
        required.update((*_VOICE_REPORTS, (VOICE_COMMAND_REPORT, 2)))
    if not required <= reports.keys():
        raise ValueError("遥控器缺少所需的按键或语音报告")
    return stream, reports


async def _read_notifications(stream, reports, emit, lost):
    queue = asyncio.Queue(maxsize=512)
    overflow = False
    microphone_requested = False

    def receive(report, data):
        nonlocal overflow
        if lost.is_set():
            return
        try:
            queue.put_nowait((report, data))
        except asyncio.QueueFull:
            overflow = True
            lost.set()

    output = reports.get((VOICE_COMMAND_REPORT, 2))
    for (report, kind), characteristic in reports.items():
        if kind == 1:
            await asyncio.wait_for(characteristic.subscribe(lambda data, report=report: receive(report, data)), _TIMEOUT)

    async def receive_reports():
        nonlocal microphone_requested
        clock = asyncio.get_running_loop().time
        deadline = None
        while not lost.is_set():
            remaining = None if deadline is None else max(0, deadline - clock())
            if remaining == 0:
                event = stream.finish_voice() if stream.ending else stream.stop_voice()
            else:
                try:
                    report, data = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    continue
                if stream.ending and stream.voice_key(report, data) is True:
                    emit(stream.finish_voice())
                event = stream.feed(report, data)
            if event.command:
                if event.command == b"\x01":
                    microphone_requested = True
                await asyncio.wait_for(output.write_value(event.command, with_response=True), _TIMEOUT)
                if event.command == b"\x00":
                    microphone_requested = False
                deadline = clock() + (VOICE_TAIL_SECONDS if stream.ending else _MAX_VOICE_SECONDS)
            if event.voice is False:
                deadline = None
            if event.keys or event.voice is not None or event.pcm:
                emit(event)

    reader = asyncio.create_task(receive_reports())
    disconnected = asyncio.create_task(lost.wait())
    try:
        await asyncio.wait((reader, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if overflow:
            raise RuntimeError("BLE 通知积压，已结束此次采集")
        if reader.done():
            reader.result()
    finally:
        for task in (reader, disconnected):
            task.cancel()
        await asyncio.gather(reader, disconnected, return_exceptions=True)
        finished = stream.disconnect()
        if finished.voice is not None:
            emit(finished)
        if microphone_requested and (not lost.is_set() or overflow):
            with suppress(Exception):
                await asyncio.wait_for(output.write_value(b"\x00", with_response=True), 2)


async def capture(adapter, config, peer_address, emit, *, microphone=False, pair=False):
    """Use any WinUSB-bound USB Bluetooth HCI adapter and a caller-specified peer."""
    resolved = parse_adapter_spec(adapter)
    from bumble.core import UUID
    from bumble.device import Device, Peer
    from bumble.transport import open_transport

    async with await open_transport(resolved) as transport:
        device = Device.from_config_file_with_hci(config, transport.source, transport.sink)
        await asyncio.wait_for(device.power_on(), _TIMEOUT)
        connection = await asyncio.wait_for(device.connect(peer_address), 15)
        lost = asyncio.Event()
        connection.on("disconnection", lambda *_: lost.set())
        try:
            if pair:
                await asyncio.wait_for(connection.pair(), 20)
            if not connection.is_encrypted:
                await asyncio.wait_for(connection.encrypt(), _TIMEOUT)
            stream, reports = await asyncio.wait_for(_discover(Peer(connection), UUID.from_16_bits, microphone), _TIMEOUT)
            await _read_notifications(stream, reports, emit, lost)
        finally:
            if not lost.is_set():
                with suppress(Exception):
                    await asyncio.wait_for(connection.disconnect(), 2)


def main():
    parser = argparse.ArgumentParser(description="BLE 遥控器 FIBER0D 实验采集")
    parser.add_argument("config", help="Bumble 本机配置文件路径")
    parser.add_argument("peer", help="已确认的遥控器蓝牙地址")
    parser.add_argument(
        "--adapter", default="auto",
        help="外接 USB 蓝牙棒（默认 auto 自动发现已绑 WinUSB 的蓝牙棒，也可写 usb:<VID>:<PID>）",
    )
    parser.add_argument("--pair", action="store_true", help="连接后配对；用于已经进入配对模式的遥控器")
    parser.add_argument("--microphone", action="store_true", help="从语音键启动收音并统计解码帧")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("实时采集入口目前用于 Windows 实验环境")
    frames = 0

    def report(event):
        nonlocal frames
        if event.voice is True:
            frames = 0
            print("遥控器语音已开始", flush=True)
        elif event.voice is False:
            print(f"遥控器语音已结束，解码 {frames} 帧", flush=True)
        frames += bool(event.pcm)
        for key, down in event.keys:
            print(key, "按下" if down else "松开", flush=True)

    try:
        asyncio.run(capture(args.adapter, args.config, args.peer, report, microphone=args.microphone, pair=args.pair))
    except KeyboardInterrupt:
        return 0
    except ImportError:
        parser.error("实时采集需要已有的 Bumble 实验环境")
    except (RuntimeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

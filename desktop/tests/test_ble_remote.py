import asyncio
from hashlib import sha256
import struct

import pytest

from remotehub import ble_remote, ble_remote_capture


_KEYBOARD = bytes.fromhex("050715002565190029657508950185018100")
_DOWN = b"\x82\x03\x01" + bytes(17)
_UP = b"\x82\x03\x00" + bytes(17)


def _packets(sequence):
    header = struct.pack("<H", sequence)
    first = header + b"\x00\x00" + b"a" * 16
    second = header + b"\x01\x00" + b"b" * 16
    third = header + b"\x02\x00" + b"c" * 8 + bytes(2) + header + second[16:20]
    return first, second, third


class _Decoder:
    def reset(self):
        pass

    def decode(self, frame):
        assert frame == b"a" * 16 + b"b" * 16 + b"c" * 8
        return b"pcm"


@pytest.fixture
def stream(monkeypatch):
    monkeypatch.setattr(ble_remote, "_REPORT_MAP_SHA256", sha256(_KEYBOARD).hexdigest())
    monkeypatch.setattr(ble_remote, "Decoder", _Decoder)
    return ble_remote.BLERemoteReports(ble_remote.VERIFIED_FIRMWARE, _KEYBOARD, use_remote_mic=True)


def test_variant_validation_and_descriptor_keyboard_edges(stream):
    with pytest.raises(ValueError, match="尚未验证"):
        ble_remote.BLERemoteReports("unknown", _KEYBOARD, use_remote_mic=False)
    assert stream.feed(1, bytes([30])).keys == (("hid:7:30", True),)
    assert stream.feed(1, bytes([0])).keys == (("hid:7:30", False),)


def test_voice_reassembly_drains_tail_before_finish_and_resets_after_loss(stream):
    assert not stream.feed(0xfc, _packets(0)[0]).pcm
    assert stream.feed(0xf8, _DOWN).command == b"\x01"
    assert stream.feed(0xf8, _DOWN).command == b""
    first, second, third = _packets(65535)
    assert not stream.feed(0xfc, first).pcm
    assert not stream.feed(0xfc, second).pcm
    assert stream.feed(0xfc, third).pcm == b"pcm"
    stop = stream.feed(0xf8, _UP)
    assert stop.command == b"\x00" and stop.voice is None
    for packet in _packets(0)[:2]:
        assert not stream.feed(0xfc, packet).pcm
    assert stream.feed(0xfc, _packets(0)[2]).pcm == b"pcm"
    assert stream.fragments.errors == 0
    assert stream.finish_voice().voice is False
    assert not stream.feed(0xfc, _packets(1)[0]).pcm

    stream.feed(0xf8, _DOWN)
    stream.feed(0xfc, _packets(3)[0])
    assert not stream.feed(0xfc, _packets(4)[1]).pcm
    assert stream.fragments.errors == 1
    assert stream.disconnect().voice is False
    assert not stream.active


def test_live_consumer_sends_start_and_stop_and_waits_for_tail(stream, monkeypatch):
    async def run():
        lost = asyncio.Event()
        subscribed = asyncio.Event()
        commands = []
        events = []

        class Characteristic:
            async def subscribe(self, callback):
                self.send = callback
                if self is audio:
                    subscribed.set()

            async def write_value(self, command, with_response=False):
                assert with_response
                commands.append(command)

        request, audio, output = Characteristic(), Characteristic(), Characteristic()
        reports = {(0xf8, 1): request, (0xfc, 1): audio, (0xfb, 2): output}

        def emit(event):
            events.append(event)
            if event.voice is False:
                lost.set()

        session = asyncio.create_task(ble_remote_capture._read_notifications(stream, reports, emit, lost))
        await asyncio.wait_for(subscribed.wait(), 1)
        request.send(_DOWN)
        request.send(_UP)
        for packet in _packets(1):
            audio.send(packet)
        await asyncio.wait_for(session, 1)
        assert commands == [b"\x01", b"\x00"]
        assert [(event.voice, event.pcm) for event in events] == [(True, b""), (None, b"pcm"), (False, b"")]

    monkeypatch.setattr(ble_remote_capture, "VOICE_TAIL_SECONDS", 0.01)
    asyncio.run(run())

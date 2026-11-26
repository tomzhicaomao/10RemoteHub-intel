"""One-shot configuration helper for WeChat Input (微信输入法) voice input."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .remote_library import RECEIVER_MODEL, RemoteLibraryStore


def default_config_dir() -> Path:
    from platformdirs import user_config_path

    return Path(user_config_path("RemoteHub", appauthor=False))


def wetype_defaults(platform: str = sys.platform) -> dict[str, str]:
    if platform == "darwin":
        return {
            "voice_keys": "fn",
            "voice_mode": "hold",
            "record_mode": "hold",
            "input_routing": "automatic",
        }
    if platform == "win32":
        return {
            "voice_keys": "ctrl+cmd",
            "voice_mode": "hold",
            "record_mode": "hold",
            "input_routing": "automatic",
        }
    raise RuntimeError("当前平台暂未提供微信输入法自动预设，请在语音设置页手动录制快捷键。")


def configure_wetype(
    config_dir: Path | None = None,
    *,
    platform: str = sys.platform,
    virtual_mic_ready: bool | None = None,
) -> dict:
    """Persist ready-to-use WeChat Input voice settings for all saved remotes."""
    target_dir = Path(config_dir) if config_dir is not None else default_config_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    store = RemoteLibraryStore(target_dir)
    store.load()
    if store.load_error:
        raise RuntimeError(store.load_error)

    if virtual_mic_ready is None:
        from .virtual_mic import VirtualMic

        virtual_mic_ready = VirtualMic().installed()

    defaults = wetype_defaults(platform)
    updated_remotes = []
    for remote in store.remotes:
        use_remote_mic = bool(
            virtual_mic_ready and remote.supports_remote_mic_for(store.readers)
        )
        source = "remote" if use_remote_mic else "trigger"
        remote.voice.update({**defaults, "voice_source": source})
        if remote.id == store.active:
            store.settings.update(remote.voice)
        updated_remotes.append({"id": remote.id, "name": remote.name, "voice_source": source})

    if not store.remotes:
        store.settings.update({**defaults, "voice_source": "trigger"})

    store.save()
    return {
        "config_dir": str(target_dir),
        "virtual_mic_ready": bool(virtual_mic_ready),
        "settings": {**defaults, "voice_source": store.settings.get("voice_source", "trigger")},
        "remotes": updated_remotes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键配置 10RemoteHub 微信输入法语音设置")
    parser.add_argument("--config-dir", type=Path, default=None, help="指定配置目录（默认使用系统用户配置目录）")
    args = parser.parse_args(argv)
    result = configure_wetype(args.config_dir)
    s = result["settings"]
    source_text = "遥控器麦克风" if s["voice_source"] == "remote" else "仅触发识别（使用电脑麦克风）"
    print("已完成微信输入法语音配置：")
    print(f"  配置目录：{result['config_dir']}")
    print(f"  语音来源：{source_text}")
    print(f"  识别快捷键：{s['voice_keys']}（{s['voice_mode']}）")
    print(f"  遥控器操作：{s['record_mode']} | 麦克风路由：{s['input_routing']}")
    if not result["virtual_mic_ready"] and sys.platform == "darwin":
        print("  提示：未检测到虚拟麦克风组件，已先设为「仅触发识别」。如需用遥控器自带麦克风，请先运行「安装遥控器组件.command」后重新运行本脚本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

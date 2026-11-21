"""Fetch/verify hash-pinned source archives and collect Linux release notices.

Run inventory_linux_runtime.py first. Supports --cache-dir (repeatable) so
already-verified local upstream tarballs can be reused without re-downloading.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/release-materials-linux")
parser.add_argument(
    "--cache-dir",
    type=Path,
    action="append",
    default=[],
    help="Optional local directories containing already-downloaded upstream tarballs",
)
args = parser.parse_args()
BASE = args.output_dir.resolve()
BASE.mkdir(parents=True, exist_ok=True)
OUT = BASE / "notices"
OUT.mkdir(exist_ok=True)
CACHE_DIRS = [p.resolve() for p in args.cache_dir if p.exists()]
components = []


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(archive: Path, name: str, qt: bool = False) -> list[str]:
    target = OUT / name
    target.mkdir(exist_ok=True)
    result: list[str] = []
    referenced: set[str] = set()
    members: set[str] = set()
    modules = {
        "qtbase",
        "qtdeclarative",
        "qtmultimedia",
        "qtimageformats",
        "qtsvg",
        "qtshadertools",
        "qttranslations",
        "qtwayland",
    }

    def included(path: str) -> bool:
        parts = PurePosixPath(path).parts
        return not qt or (len(parts) > 1 and parts[1] in modules and not ({"examples", "tests"} & set(parts)))

    def copy(tar, member):
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe archive path")
        dest = target.joinpath(*relative.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = tar.extractfile(member).read()
        dest.write_bytes(data)
        result.append(os.path.relpath(dest, BASE))
        return data

    with tarfile.open(archive, "r|*") as tar:
        for member in tar:
            if not member.isfile() or not included(member.name):
                continue
            leaf = PurePosixPath(member.name).name
            if re.match(r"^(license|licence|copying|copyright|notice|authors)([._-]|$)", leaf, re.I) or leaf == "qt_attribution.json":
                data = copy(tar, member)
                members.add(member.name)
                if leaf == "qt_attribution.json":
                    entries = json.loads(data, strict=False)
                    if isinstance(entries, dict):
                        entries = [entries]
                    for entry in entries:
                        files = entry.get("LicenseFile", [])
                        if isinstance(files, str):
                            files = [files]
                        for file in files:
                            referenced.add(os.path.normpath(str(PurePosixPath(member.name).parent / file)))
    if referenced - members:
        with tarfile.open(archive, "r|*") as tar:
            for member in tar:
                if member.isfile() and member.name in referenced - members:
                    copy(tar, member)
                    members.add(member.name)
        missing = referenced - members
        if missing:
            raise RuntimeError("Missing attribution license files: " + repr(sorted(missing)))
    return result


pinned = json.loads((ROOT / "desktop/licenses/linux-sources.json").read_text(encoding="utf-8"))["archives"]
source_dir = BASE / "sources"
source_dir.mkdir(exist_ok=True)

for item in pinned:
    destination = source_dir / item["filename"]
    if not destination.exists():
        for cache_root in CACHE_DIRS:
            candidate = cache_root / item["filename"]
            if candidate.is_file() and sha256(candidate) == item["sha256"]:
                shutil.copy2(candidate, destination)
                break
    if destination.exists():
        if sha256(destination) != item["sha256"]:
            raise RuntimeError("Existing source checksum mismatch: " + str(destination))
    else:
        fd, temporary = tempfile.mkstemp(prefix=".source-", dir=source_dir)
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(item["url"], timeout=60) as response:
                total = 0
                while chunk := response.read(1 << 20):
                    total += len(chunk)
                    if total > item["size"]:
                        raise RuntimeError("Source exceeds pinned size: " + item["filename"])
                    out.write(chunk)
            if sha256(Path(temporary)) != item["sha256"]:
                raise RuntimeError("Downloaded source checksum mismatch: " + item["filename"])
            Path(temporary).replace(destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print("Source verified:", item["filename"], flush=True)

native = json.loads((BASE / "native-inventory-linux.json").read_text(encoding="utf-8"))
if native["qt"] != "6.12.0" or native["pyside"] != "6.12.0" or not native["python"].startswith("3.12.13"):
    raise RuntimeError("Runtime differs from pinned source baseline; review sources before building")
if not native["ffmpeg"]:
    raise RuntimeError("No FFmpeg runtime provenance found")
for runtime in native["ffmpeg"]:
    if (
        "--prefix=/usr/local/FFmpeg-n9.0.1" not in runtime["configuration"]
        or "--enable-gpl" in runtime["configuration"]
        or "--enable-nonfree" in runtime["configuration"]
        or "LGPL" not in runtime["license"]
    ):
        raise RuntimeError("FFmpeg differs from reviewed source/license baseline")

catalog = [item for item in pinned if item["name"] not in ("qt", "pyside6", "ffmpeg")]
for item in catalog:
    files = extract(source_dir / item["filename"], item["name"])
    row = {
        "name": item["name"],
        "version": item["version"],
        "source_url": item["url"],
        "source_sha256": item["sha256"],
        "license_files": files,
        "build_notes": (
            "Official source archive; dependency binary and build provenance are recorded "
            "in native-inventory-linux.json. Rebuild instructions are in desktop/RELEASE.md."
        ),
    }
    components.append(row)

qt_archive = source_dir / "qt-everywhere-src-6.12.0.tar.xz"
qt_hash = sha256(qt_archive)
assert qt_hash == base64.b64decode("mP9PRLrG7D4eYu4qQxauDj0VutsBXXU8wCaKINtS8WU=").hex()
components.append({
    "name": "qt",
    "version": "6.12.0",
    "source_url": "https://download.qt.io/official_releases/qt/6.12/6.12.0/single/" + qt_archive.name,
    "source_sha256": qt_hash,
    "license_files": extract(qt_archive, "qt", qt=True),
    "license_root": "notices/qt",
    "build_notes": (
        "Qt 6.12.0 (Xcb + Wayland client + Multimedia), qVersion confirmed on the target Linux x86_64 host. "
        "Distributed by official PySide6 6.12.0 manylinux_2_34_x86_64 wheels. Only reviewed Widgets/Multimedia "
        "and platform integration modules are bundled."
    ),
})

vendor = ROOT / "desktop/licenses/vendor"
upstream = json.loads((vendor / "SOURCES.json").read_text(encoding="utf-8"))["upstream_archives"]
for item in upstream:
    names = ["pyside6", "pyside6-addons", "pyside6-essentials", "shiboken6"] if item["name"] == "pyside6" else ["ffmpeg"]
    for name in names:
        row = {
            "name": name,
            "version": item["version"],
            "source_url": item["source_url"],
            "source_sha256": item["source_sha256"],
            "license_files": [os.path.relpath(vendor / n["path"], BASE) for n in item["notices"]],
            "build_notes": (
                "Official PySide6 6.12.0 manylinux_2_34_x86_64 wheel; upstream pyside-setup archive contains "
                "the binding generator, build system and sources. Shared libraries are dynamically loaded without source patches."
            ),
        }
        if name == "ffmpeg":
            ff = native["ffmpeg"][0]
            row["build_notes"] = (
                "Loaded target ELF binary reports LGPL-2.1-or-later, FFmpeg n9.0.1. Configuration: "
                + ff["configuration"]
                + ". Official 9.0.1 source includes configure/build files. No GPL/nonfree switches enabled."
            )
        components.append(row)

for pattern in ("lib/python3.12/LICENSE.txt", "LICENSE.txt"):
    python_notice = Path(sys.base_prefix) / pattern
    if python_notice.is_file():
        dest = OUT / "python-runtime-LICENSE.txt"
        shutil.copy2(python_notice, dest)
        next(c for c in components if c["name"] == "python")["license_files"].append(os.path.relpath(dest, BASE))
        break

next(c for c in components if c["name"] == "python")["build_notes"] = (
    "CPython 3.12.13 x86_64-unknown-linux-gnu. Official Python 3.12.13 upstream tarball and runtime license "
    "included; PyInstaller bundles libpython3.12.so.1.0 as an unpatched shared library."
)
next(c for c in components if c["name"] == "libusb")["build_notes"] = (
    "Runtime libusb 1.0.30 comes from libusb-package 1.0.30.0; dynamically loaded by PyUSB as a standalone .so."
)
next(c for c in components if c["name"] == "python-xlib")["build_notes"] = (
    "Pure-Python LGPLv2.1+ X11 client library (python-xlib 0.33); upstream source archive and license notices included."
)
next(c for c in components if c["name"] == "evdev")["build_notes"] = (
    "BSD-3-Clause Linux input/uinput binding (evdev 2.0.0); upstream source archive and license included."
)

supplement_path = BASE / "license-supplement-linux.json"
supplement_path.write_text(json.dumps({"components": components}, indent=2) + "\n", encoding="utf-8")
print(
    json.dumps(
        {
            "supplement": str(supplement_path),
            "components": len(components),
            "notices": sum(len(c["license_files"]) for c in components),
            "qt_sha256": qt_hash,
        }
    ),
    flush=True,
)

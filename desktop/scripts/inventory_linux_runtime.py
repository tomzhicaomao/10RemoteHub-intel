"""Read-only Linux build provenance. Does not enumerate USB or alter audio.

Raw paths belong in the local material directory, not in a public release.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata as m
import json
from pathlib import Path
import platform
import sys

import libusb_package
import PySide6
from PySide6.QtCore import qVersion

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--output-dir",
    type=Path,
    default=Path(__file__).resolve().parents[2] / "artifacts/release-materials-linux",
)
output = parser.parse_args().output_dir.resolve()
output.mkdir(parents=True, exist_ok=True)

qt = Path(PySide6.__file__).parent / "Qt"
qt_lib = qt / "lib"

report = {
    "python": sys.version,
    "platform": platform.platform(),
    "machine": platform.machine(),
    "qt": qVersion(),
    "pyside": PySide6.__version__,
    "packages": sorted(
        [{"name": d.metadata["Name"], "version": d.version} for d in m.distributions()],
        key=lambda x: x["name"].lower(),
    ),
    "ffmpeg": [],
}

# Preload Qt FFmpeg OpenSSL/VAAPI stub libraries so libavcodec resolves cleanly in isolation.
for stub in sorted(qt_lib.glob("libQt6FFmpegStub-*.so*")):
    if not stub.is_symlink():
        ctypes.CDLL(str(stub), mode=ctypes.RTLD_GLOBAL)
for dep in ("libavutil.so", "libswresample.so", "libswscale.so"):
    for dep_path in sorted(qt_lib.glob(dep + "*")):
        if not dep_path.is_symlink():
            ctypes.CDLL(str(dep_path), mode=ctypes.RTLD_GLOBAL)

for path in sorted(qt_lib.glob("libavcodec.so*")):
    if path.is_symlink():
        continue
    lib = ctypes.CDLL(str(path))
    row = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    for field in ("configuration", "license"):
        fn = getattr(lib, "avcodec_" + field)
        fn.restype = ctypes.c_char_p
        row[field] = fn().decode()
    fn = lib.avcodec_version
    fn.restype = ctypes.c_uint
    v = fn()
    row["abi_version"] = ".".join(str(n) for n in (v >> 16, (v >> 8) & 255, v & 255))
    report["ffmpeg"].append(row)

libusb_so = libusb_package.get_library_path()
report["libusb_wheel"] = {
    "file": Path(libusb_so).name if libusb_so else None,
    "sha256": hashlib.sha256(Path(libusb_so).read_bytes()).hexdigest() if libusb_so and Path(libusb_so).is_file() else None,
}
report["python_notice_files"] = [
    str(p)
    for pattern in ("LICENSE*", "lib/python*/LICENSE*", "share/doc/python*/LICENSE*")
    for p in Path(sys.base_prefix).glob(pattern)
]

out_path = output / "native-inventory-linux.json"
out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(
    json.dumps(
        {
            "report": str(out_path),
            "python": platform.python_version(),
            "qt": report["qt"],
            "ffmpeg": report["ffmpeg"],
            "libusb": report["libusb_wheel"],
        }
    )
)

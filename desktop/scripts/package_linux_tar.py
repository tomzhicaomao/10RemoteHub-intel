"""Create a deterministic Linux release tar.gz without workstation metadata."""
from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
import tarfile


def _normalized_mode(path: Path, root: Path) -> int:
    rel = path.relative_to(root)
    is_main_bin = rel.parts == (root.name, root.name) or (len(rel.parts) == 1 and rel.name == root.name)
    is_exec = is_main_bin or ".so" in path.name or bool(path.stat().st_mode & 0o111)
    return 0o755 if is_exec else 0o644


def package(source: Path, destination: Path) -> Path:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.is_relative_to(source):
        raise ValueError("Place release tar.gz outside the application directory")
    prefix = source.name
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for path in sorted(source.rglob("*")):
                    rel = path.relative_to(source).as_posix()
                    arcname = f"{prefix}/{rel}"
                    if path.is_symlink():
                        link_target = os.readlink(path)
                        resolved = (path.parent / link_target).resolve()
                        if Path(link_target).is_absolute() or not resolved.is_relative_to(source):
                            raise ValueError(f"Linux release symlink escapes bundle: {rel} -> {link_target}")
                        info = tarfile.TarInfo(arcname)
                        info.type = tarfile.SYMTYPE
                        info.linkname = link_target
                        info.mode = 0o777
                        tar.addfile(info)
                    elif path.is_file():
                        data = path.read_bytes()
                        info = tarfile.TarInfo(arcname)
                        info.size = len(data)
                        info.mode = _normalized_mode(path, source)
                        # TarInfo defaults: uid=0, gid=0, uname='', gname='', mtime=0, no xattrs.
                        tar.addfile(info, io.BytesIO(data))
    return destination


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(package(args.source, args.destination))

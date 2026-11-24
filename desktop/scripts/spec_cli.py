"""Add the console ``10remotehub-cli`` executable to a PyInstaller spec.

The CLI shares the application's Analysis, so both executables use the same
bundled runtime (``_internal`` on Windows, ``Contents`` on macOS). ``entry.py``
picks the CLI when it runs under the CLI executable's name.
"""
import re

CLI_NAME = "10remotehub-cli"


def add_cli_executable(spec: str, app_name: str) -> str:
    match = re.search(r"^exe = EXE\(\n.*?^\)\n", spec, re.S | re.M)
    if match is None or f"name='{app_name}'," not in match.group(0):
        raise ValueError("PyInstaller spec has no application EXE block")
    block = match.group(0)
    cli = (block.replace("exe = EXE(", "cli_exe = EXE(", 1)
           .replace(f"name='{app_name}',", f"name='{CLI_NAME}',", 1)
           .replace("console=False,", "console=True,", 1))
    if "console=True," not in cli:
        raise ValueError("PyInstaller spec EXE block has no console flag")
    spec = spec[:match.end()] + cli + spec[match.end():]
    collect = "coll = COLLECT(\n    exe,\n"
    if collect not in spec:
        raise ValueError("PyInstaller spec has no COLLECT block")
    return spec.replace(collect, collect + "    cli_exe,\n", 1)

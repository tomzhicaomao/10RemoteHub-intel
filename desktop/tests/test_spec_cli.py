"""The packaged app ships a console 10remotehub-cli next to the GUI executable."""
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

DESKTOP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP / "scripts"))

from spec_cli import CLI_NAME, add_cli_executable  # noqa: E402


def makespec(tmp_path, name="10RemoteHub"):
    subprocess.run([sys.executable, "-m", "PyInstaller.utils.cliutils.makespec", "--windowed", "--onedir",
                    "--name", name, "--specpath", str(tmp_path), str(DESKTOP / "entry.py")],
                   check=True, capture_output=True)
    return (tmp_path / f"{name}.spec").read_text(encoding="utf-8")


def test_spec_gets_a_console_cli_sharing_the_app_runtime(tmp_path):
    spec = add_cli_executable(makespec(tmp_path), "10RemoteHub")
    compile(spec, "10RemoteHub.spec", "exec")
    assert spec.count("= EXE(") == 2 and spec.count("Analysis(") == 1
    cli = spec[spec.index("cli_exe = EXE("):]
    cli = cli[:cli.index("\n)\n")]
    assert f"name='{CLI_NAME}'," in cli and "console=True," in cli
    app = spec[spec.index("\nexe = EXE("):spec.index("cli_exe = EXE(")]
    assert "name='10RemoteHub'," in app and "console=False," in app
    assert "coll = COLLECT(\n    exe,\n    cli_exe,\n" in spec


def test_unexpected_spec_layout_fails_loudly():
    with pytest.raises(ValueError):
        add_cli_executable("a = Analysis([])\n", "10RemoteHub")


@pytest.mark.parametrize("executable, expected", [("10remotehub-cli", "remotehub.cli"),
                                                  ("10RemoteHub-CLI.exe", "remotehub.cli"),
                                                  ("10RemoteHub", "remotehub.app")])
def test_entry_point_picks_the_cli_by_executable_name(monkeypatch, executable, expected):
    called = []
    for module in ("remotehub.cli", "remotehub.app"):
        monkeypatch.setattr(f"{module}.main", lambda m=module: called.append(m) or 0)
    monkeypatch.setattr(sys, "executable", str(Path("/bundle") / executable))
    with pytest.raises(SystemExit):
        runpy.run_path(str(DESKTOP / "entry.py"))
    assert called == [expected]

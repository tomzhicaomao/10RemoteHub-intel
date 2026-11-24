"""Agent CLI (remotehub.cli): offline against remotes.json, online against a running main program."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

import pytest

import remotehub
from remotehub import cli, cli_bridge, cli_service
from remotehub.model import Action, Profile
from remotehub.remote_library import RemoteLibraryStore

DESKTOP = Path(remotehub.__file__).resolve().parents[1]
SAVE = '{"kind":"shortcut","keys":["cmd","s"]}'
COPY = '{"kind":"common","target":"copy"}'


@pytest.fixture(autouse=True)
def private_channel(monkeypatch, qapp):
    """Never talk to a real 10RemoteHub on this machine: every test gets its own channel name."""
    name = f"rh-cli-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("REMOTEHUB_CLI_INSTANCE", name)
    return name


def run_cli(capsys, tmp_path, *argv):
    code = cli.main([*argv, "--config-dir", str(tmp_path)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, tmp_path, *argv):
    code, out, err = run_cli(capsys, tmp_path, *argv, "--json")
    assert not err
    return code, json.loads(out)


def load(tmp_path):
    store = RemoteLibraryStore(tmp_path)
    store.load()
    assert not store.load_error
    return store


def active_profile(tmp_path):
    store = load(tmp_path)
    return next(p for p in store.remote.profiles if p.id == store.settings["active_profile_id"])


def key(profile, identity):
    return next(b for b in profile.buttons if b.id == identity)


def initialised(capsys, tmp_path):
    """Write remotes.json once (migrated from the 0.2.0 profiles.json the conftest provides)."""
    code, _ = run_json(capsys, tmp_path, "keys", "set", "tv-number-1", "--action", COPY)
    assert code == 0
    return (tmp_path / "remotes.json").read_bytes()


# -- offline -----------------------------------------------------------------------

def test_status_and_keys_list_offline(capsys, tmp_path):
    code, status = run_json(capsys, tmp_path, "status")
    assert code == 0 and status["ok"] and status["mode"] == "offline"
    assert status["remote"]["connections"] == [{"transport": "USB", "id": "0416:0602"}]
    assert status["profile"]["keys"] == 30
    assert status["connection"]["state"] == "主程序未运行"
    assert status["input_path"]["id"] == "receiver"

    code, listed = run_json(capsys, tmp_path, "keys", "list")
    assert code == 0
    keys = {item["id"]: item for item in listed["keys"]}
    assert keys["tv-power"]["configurable"] is False and "禁配" in keys["tv-power"]["fixed"]
    assert keys["tv-voice"]["configurable"] is False and "语音" in keys["tv-voice"]["fixed"]
    assert keys["tv-up"]["code"] == "hid:7:82" and keys["tv-up"]["tap"] is None
    assert keys["tv-up"]["codes"] == ["hid:7:82"]
    assert set(keys["tv-up"]) >= {"id", "name", "code", "codes", "tap", "double", "hold", "layers"}

    code, out, _ = run_cli(capsys, tmp_path, "keys", "list")
    assert code == 0 and "id=tv-up" in out and "单击=未设置" in out
    assert not (tmp_path / "remotes.json").exists()       # reading never writes


def test_set_and_clear_a_key_offline_with_backup(capsys, tmp_path):
    initialised(capsys, tmp_path)
    code, result = run_json(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE)
    assert code == 0 and result["changed"] and not result["dry_run"]
    assert result["changes"][0]["key"] == "tv-number-3" and result["changes"][0]["after"]["keys"] == ["cmd", "s"]
    assert key(active_profile(tmp_path), "tv-number-3").action == Action("shortcut", ["cmd", "s"])
    assert list((tmp_path / "cli-backups").glob("remotes-*.json"))

    code, result = run_json(capsys, tmp_path, "keys", "set", "3", "--gesture", "hold", "--action", COPY)  # by name
    assert code == 0
    assert key(active_profile(tmp_path), "tv-number-3").long_action == Action("common", target="copy")

    code, result = run_json(capsys, tmp_path, "keys", "clear", "tv-number-3", "--gesture", "hold")
    assert code == 0 and result["changes"][0]["after"] is None
    button = key(active_profile(tmp_path), "tv-number-3")
    assert button.long_action == Action() and button.action == Action("shortcut", ["cmd", "s"])

    code, out, _ = run_cli(capsys, tmp_path, "keys", "set", "tv-number-4", "--action", SAVE)
    assert code == 0 and "主程序未运行，已直接读写配置文件" in out


def test_dry_run_never_changes_the_file(capsys, tmp_path):
    before = initialised(capsys, tmp_path)
    stamp = (tmp_path / "remotes.json").stat().st_mtime_ns
    for argv in (
        ("keys", "set", "tv-number-3", "--action", SAVE),
        ("keys", "clear", "tv-number-1"),
        ("layers", "set", "com.example.editor", "tv-number-3", "--action", COPY),
        ("profiles", "use", "tv-photo"),
    ):
        code, result = run_json(capsys, tmp_path, *argv, "--dry-run")
        assert code == 0 and result["dry_run"], argv
    code, result = run_json(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE, "--dry-run")
    assert result["changes"] and result["changes"][0]["after"]["label"]
    code, out, _ = run_cli(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE, "--dry-run")
    assert out.startswith("【试运行，未修改】")
    assert (tmp_path / "remotes.json").read_bytes() == before
    assert (tmp_path / "remotes.json").stat().st_mtime_ns == stamp


@pytest.mark.parametrize("argv, code, words", [
    (("keys", "set", "tv-number-3", "--action", "not json"), 2, "JSON"),
    (("keys", "set", "tv-number-3", "--action", '{"kind":"shortcut","keys":["cmd","cmd"]}'), 4, "组合键"),
    (("keys", "set", "tv-number-3", "--action", '{"kind":"teleport"}'), 4, "未知动作"),
    (("keys", "set", "tv-number-3", "--action", '{"kind":"text","colour":"red"}'), 2, "字段"),
    (("keys", "set", "tv-number-3", "--gesture", "double", "--action", '{"kind":"mouse","target":"left"}'), 4, "鼠标"),
    (("keys", "set", "tv-number-3", "--gesture", "hold", "--action", '{"kind":"voice"}'), 4, "语音"),
    (("keys", "set", "nosuch", "--action", SAVE), 3, "keys list"),
    (("keys", "sett", "tv-number-3"), 2, "--help"),
    (("layers", "set", "a/b", "tv-number-3", "--action", SAVE), 4, "应用标识"),
])
def test_invalid_requests_are_rejected_with_a_fix(capsys, tmp_path, argv, code, words):
    before = initialised(capsys, tmp_path)
    actual, result = run_json(capsys, tmp_path, *argv)
    assert actual == code
    assert result["ok"] is False and result["error"]["code"] == code
    assert words in result["error"]["message"] + result["error"]["hint"]
    assert result["error"]["hint"]
    assert (tmp_path / "remotes.json").read_bytes() == before
    actual, out, err = run_cli(capsys, tmp_path, *argv)
    assert actual == code and not out and err.startswith("错误：") and "怎么改：" in err


@pytest.mark.parametrize("argv", [
    ("keys", "set", "tv-voice", "--action", SAVE),
    ("keys", "clear", "tv-voice"),
    ("keys", "set", "tv-voice", "--gesture", "double", "--action", COPY),
    ("layers", "set", "com.example.editor", "tv-voice", "--action", '{"kind":"voice"}'),
    ("keys", "set", "tv-power", "--action", SAVE),
    ("keys", "set", "tv-tv", "--action", '{"kind":"none"}'),
    ("layers", "set", "com.example.editor", "tv-power", "--action", SAVE),
])
def test_fixed_keys_cannot_be_changed(capsys, tmp_path, argv):
    before = initialised(capsys, tmp_path)
    code, result = run_json(capsys, tmp_path, *argv)
    assert code == cli_service.REJECTED
    assert "语音键" in result["error"]["message"] or "禁配键" in result["error"]["message"]
    assert (tmp_path / "remotes.json").read_bytes() == before


def test_voice_key_keeps_its_voice_action(capsys, tmp_path):
    initialised(capsys, tmp_path)
    code, result = run_json(capsys, tmp_path, "keys", "set", "tv-voice", "--action", '{"kind":"voice"}')
    assert code == 0 and not result["changed"]


def test_mouse_takes_the_whole_key(capsys, tmp_path):
    initialised(capsys, tmp_path)
    run_json(capsys, tmp_path, "keys", "set", "tv-number-5", "--gesture", "hold", "--action", COPY)
    code, result = run_json(capsys, tmp_path, "keys", "set", "tv-number-5", "--action", '{"kind":"mouse","target":"left"}')
    assert code == 0
    assert [c["gesture"] for c in result["changes"]] == ["tap", "hold"]
    button = key(active_profile(tmp_path), "tv-number-5")
    assert button.action.kind == "mouse" and button.long_action == Action()


def test_app_layers_offline(capsys, tmp_path):
    initialised(capsys, tmp_path)
    code, result = run_json(capsys, tmp_path, "layers", "set", "com.example.editor", "tv-number-3",
                            "--action", SAVE, "--name", "编辑器")
    assert code == 0 and result["created"]
    profile = active_profile(tmp_path)
    layer = profile.app_layers[0]
    assert (layer.app_id, layer.name) == ("com.example.editor", "编辑器")
    assert layer.overrides["tv-number-3"]["single"] == Action("shortcut", ["cmd", "s"])

    code, result = run_json(capsys, tmp_path, "layers", "set", "编辑器", "tv-number-3", "--gesture", "double",
                            "--action", '{"kind":"none"}')
    assert code == 0 and not result["created"]
    assert active_profile(tmp_path).app_layers[0].overrides["tv-number-3"]["double"] == Action()

    code, listed = run_json(capsys, tmp_path, "layers", "list")
    assert [item["gesture"] for item in listed["layers"][0]["overrides"]] == ["tap", "double"]
    code, keys = run_json(capsys, tmp_path, "keys", "list")
    three = next(item for item in keys["keys"] if item["id"] == "tv-number-3")
    assert three["layers"][0]["app"] == "com.example.editor" and three["layers"][0]["tap"]["keys"] == ["cmd", "s"]

    for gesture in ("tap", "double"):
        assert run_json(capsys, tmp_path, "layers", "clear", "com.example.editor", "tv-number-3",
                        "--gesture", gesture)[0] == 0
    assert active_profile(tmp_path).app_layers[0].overrides == {}
    code, result = run_json(capsys, tmp_path, "layers", "clear", "com.other", "tv-number-3")
    assert code == cli_service.NOT_FOUND


def test_profiles_and_remotes_offline(capsys, tmp_path):
    initialised(capsys, tmp_path)
    store = load(tmp_path)
    second = Profile.loads(store.remote.profiles[0].dumps())
    second.id, second.name = "second", "第二套"
    store.remote.profiles.append(second)
    store.save()

    code, listed = run_json(capsys, tmp_path, "profiles", "list")
    assert [(p["id"], p["active"]) for p in listed["profiles"]] == [("tv-photo", True), ("second", False)]
    code, result = run_json(capsys, tmp_path, "profiles", "use", "第二套")
    assert code == 0 and result["changed"]
    assert load(tmp_path).settings["active_profile_id"] == "second"

    code, listed = run_json(capsys, tmp_path, "remotes", "list")
    assert code == 0 and len(listed["remotes"]) == 1 and listed["remotes"][0]["active"]
    remote_id = listed["remotes"][0]["id"]
    code, result = run_json(capsys, tmp_path, "remotes", "use", remote_id)
    assert code == 0 and not result["changed"]
    assert run_json(capsys, tmp_path, "remotes", "use", "missing")[0] == cli_service.NOT_FOUND


def test_export_import_round_trip(capsys, tmp_path):
    initialised(capsys, tmp_path)
    run_json(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE)
    run_json(capsys, tmp_path, "layers", "set", "com.example.editor", "tv-number-3", "--action", COPY)
    exported = tmp_path / "out" / "scheme.json"
    exported.parent.mkdir()
    code, result = run_json(capsys, tmp_path, "export", str(exported))
    assert code == 0 and result["file"] == "scheme.json" and str(tmp_path) not in json.dumps(result)
    original = active_profile(tmp_path)
    assert Profile.loads(exported.read_text(encoding="utf-8")).buttons == original.buttons

    code, result = run_json(capsys, tmp_path, "import", str(exported))
    assert code == 0 and result["changed"]
    store = load(tmp_path)
    assert len(store.remote.profiles) == 2
    imported = active_profile(tmp_path)
    assert imported.id != original.id and imported.name == original.name + " 2"
    assert imported.buttons == original.buttons
    assert [(l.app_id, l.overrides) for l in imported.app_layers] == [(l.app_id, l.overrides) for l in original.app_layers]

    # Export again: the same content apart from identity and name.
    again = tmp_path / "again.json"
    assert run_json(capsys, tmp_path, "export", str(again))[0] == 0
    first, second = (json.loads(path.read_text(encoding="utf-8")) for path in (exported, again))
    for data in (first, second):
        data.pop("id"), data.pop("name")
        for layer in data["app_layers"]:
            layer.pop("id")
    assert first == second

    code, result = run_json(capsys, tmp_path, "import", str(exported), "--replace")
    assert code == 0 and len(load(tmp_path).remote.profiles) == 2


def test_import_for_another_model_is_refused(capsys, tmp_path):
    before = initialised(capsys, tmp_path)
    other = tmp_path / "other.json"
    other.write_text(Profile(name="别的遥控器", vendor=0x1234, product=0x5678).dumps(), encoding="utf-8")
    code, result = run_json(capsys, tmp_path, "import", str(other))
    assert code == cli_service.REJECTED and "1234:5678" in result["error"]["message"]
    assert (tmp_path / "remotes.json").read_bytes() == before


def test_output_has_no_serial_numbers_or_personal_paths(capsys, tmp_path):
    initialised(capsys, tmp_path)
    store = load(tmp_path)
    unit = "ab" * 32
    store.remote.connections[0].units = [unit]
    store.remote.profiles[0].serial = "SERIAL-123"
    store.save()
    outputs = []
    for argv in (("status",), ("remotes", "list"), ("profiles", "list"), ("keys", "list"), ("layers", "list")):
        for extra in ((), ("--json",)):
            outputs.append(run_cli(capsys, tmp_path, *argv, *extra)[1])
    exported = tmp_path / "e.json"
    outputs.append(run_cli(capsys, tmp_path, "export", str(exported))[1])
    text = "\n".join(outputs)
    assert unit not in text and "SERIAL-123" not in text
    assert str(tmp_path) not in text and str(Path.home()) not in text
    assert "SERIAL-123" not in exported.read_text(encoding="utf-8")


def test_unreadable_configuration_is_left_alone(capsys, tmp_path):
    (tmp_path / "remotes.json").write_text("{broken", encoding="utf-8")
    code, result = run_json(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE)
    assert code == cli_service.STORAGE and result["error"]["hint"]
    assert (tmp_path / "remotes.json").read_text(encoding="utf-8") == "{broken"


def test_actions_kinds_lists_every_kind_with_parameters(capsys, tmp_path):
    from remotehub.model import KINDS

    code, result = run_json(capsys, tmp_path, "actions", "kinds")
    assert code == 0
    assert [item["kind"] for item in result["kinds"]] == list(KINDS)
    shortcut = next(item for item in result["kinds"] if item["kind"] == "shortcut")
    assert shortcut["params"][0]["name"] == "keys" and shortcut["params"][0]["required"]
    assert {"target": "copy", "name": "复制"} in result["common_targets"]
    assert next(item for item in result["kinds"] if item["kind"] == "mouse")["gestures"] == ["tap"]


# -- concurrent writers ---------------------------------------------------------------

def test_a_save_by_the_main_program_between_load_and_write_is_not_overwritten(capsys, tmp_path, monkeypatch):
    initialised(capsys, tmp_path)

    def main_program_saves(session):
        main = RemoteLibraryStore(tmp_path)
        main.load()
        main.remote.profiles[0].name = "主程序刚改的名字"
        main.save()

    monkeypatch.setattr(cli_service, "after_load", main_program_saves)
    code, result = run_json(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE)
    assert code == cli_service.STORAGE and "重新执行" in result["error"]["hint"]
    profile = active_profile(tmp_path)
    assert profile.name == "主程序刚改的名字"
    assert key(profile, "tv-number-3").action == Action()

    monkeypatch.setattr(cli_service, "after_load", lambda session: None)
    assert run_json(capsys, tmp_path, "keys", "set", "tv-number-3", "--action", SAVE)[0] == 0
    profile = active_profile(tmp_path)
    assert profile.name == "主程序刚改的名字" and key(profile, "tv-number-3").action.kind == "shortcut"


def test_cli_writers_are_serialised_by_the_lock(tmp_path, capsys):
    initialised(capsys, tmp_path)
    results = []

    def write(identity):
        args = cli_service.parse(["keys", "set", identity, "--action", SAVE, "--config-dir", str(tmp_path)])
        with cli_service.offline(tmp_path, args) as session:
            results.append(cli_service.execute(args, session)[0])

    threads = [threading.Thread(target=write, args=(f"tv-number-{n}",)) for n in range(2, 8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert results == [0] * 6
    profile = active_profile(tmp_path)
    assert all(key(profile, f"tv-number-{n}").action.kind == "shortcut" for n in range(2, 8))


def test_main_program_saving_while_the_cli_writes_never_corrupts_the_file(tmp_path, capsys):
    initialised(capsys, tmp_path)
    stop = threading.Event()
    problems = []

    def main_program():
        main = RemoteLibraryStore(tmp_path)
        main.load()
        count = 0
        while not stop.is_set():
            count += 1
            main.remote.profiles[0].name = f"主程序 {count}"
            main.save()

    def reader():
        while not stop.is_set():
            try:
                json.loads((tmp_path / "remotes.json").read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                problems.append(exc)

    workers = [threading.Thread(target=main_program), threading.Thread(target=reader)]
    for worker in workers:
        worker.start()
    codes = []
    try:
        for n in range(12):
            args = cli_service.parse(["keys", "set", f"tv-number-{n % 9 + 1}", "--action", SAVE])
            try:
                with cli_service.offline(tmp_path, args) as session:
                    codes.append(cli_service.execute(args, session)[0])
            except cli_service.CliError as exc:
                codes.append(exc.code)
    finally:
        stop.set()
        for worker in workers:
            worker.join(30)
    assert not problems
    assert set(codes) <= {cli_service.OK, cli_service.STORAGE}
    load(tmp_path)                                           # still a valid configuration


# -- online: a main program with the real controller -----------------------------------

class MainProgram:
    """The running app's side: real AppController, the instance server and cli_bridge."""

    def __init__(self, qtbot, directory, name):
        from PySide6.QtNetwork import QLocalServer
        from remotehub.controller import AppController

        self.qtbot = qtbot
        self.controller = AppController(directory, auto_mapping=False)
        self.server = QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(name)
        assert self.server.listen(name)
        self.shown = 0
        self.server.newConnection.connect(self._connection)
        self.directory, self.name = directory, name

    def _connection(self):          # same order as app.main's bring_forward
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if cli_bridge.accept(sock, self.controller):
                continue
            sock.waitForReadyRead(200)
            sock.close()
            self.shown += 1

    def cli(self, *argv, config_dir=None):
        env = {**os.environ, "PYTHONPATH": str(DESKTOP), "QT_QPA_PLATFORM": "offscreen",
               "REMOTEHUB_CLI_INSTANCE": self.name}
        extra = ["--config-dir", str(config_dir)] if config_dir else []
        process = subprocess.Popen([sys.executable, "-m", "remotehub.cli", *argv, *extra, "--json"],
                                   cwd=str(self.directory), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.qtbot.waitUntil(lambda: process.poll() is not None, timeout=60_000)
        out, err = process.communicate()
        return process.returncode, json.loads(out.decode("utf-8"))

    def close(self):
        self.server.close()
        self.controller.shutdown()


@pytest.fixture
def main_program(qtbot, tmp_path, private_channel):
    program = MainProgram(qtbot, tmp_path, private_channel)
    yield program
    program.close()


def test_online_status_comes_from_the_main_program(main_program):
    code, status = main_program.cli("status")
    assert code == 0 and status["mode"] == "online"
    view = main_program.controller.remote_view()
    assert status["connection"] == {"state": view.title, "detail": view.detail, "tone": view.tone}
    assert status["remote"]["id"] == main_program.controller.remote.id
    assert main_program.shown == 0


def test_online_key_change_takes_effect_at_once(main_program):
    controller = main_program.controller
    refreshed = []
    controller.mapping_changed.connect(lambda: refreshed.append(True))
    controller.select("tv-ok")
    code, result = main_program.cli("keys", "set", "tv-number-3", "--action", SAVE)
    assert code == 0 and result["mode"] == "online" and result["changed"]
    assert key(controller.profile, "tv-number-3").action == Action("shortcut", ["cmd", "s"])   # live object
    assert refreshed                                                                          # window refreshes
    assert controller.selected_id == "tv-ok"                                                  # selection kept
    assert key(active_profile(main_program.directory), "tv-number-3").action.kind == "shortcut"  # saved

    code, result = main_program.cli("keys", "clear", "tv-number-3")
    assert code == 0 and key(controller.profile, "tv-number-3").action == Action()


def test_online_dry_run_and_rules(main_program):
    controller = main_program.controller
    code, result = main_program.cli("keys", "set", "tv-number-3", "--action", SAVE, "--dry-run")
    assert code == 0 and result["dry_run"] and result["changes"]
    assert key(controller.profile, "tv-number-3").action == Action()
    assert not (main_program.directory / "remotes.json").exists() or \
        key(active_profile(main_program.directory), "tv-number-3").action == Action()

    for argv in (("keys", "set", "tv-voice", "--action", SAVE), ("keys", "set", "tv-power", "--action", SAVE),
                 ("keys", "set", "tv-number-3", "--gesture", "double", "--action", '{"kind":"mouse","target":"left"}')):
        code, result = main_program.cli(*argv)
        assert code == cli_service.REJECTED, argv
        assert result["mode"] == "online" and result["error"]["hint"]
    assert key(controller.profile, "tv-voice").action == Action("voice")
    assert key(controller.profile, "tv-power").action == Action()


def test_online_layers_profiles_and_import(main_program, tmp_path):
    controller = main_program.controller
    code, result = main_program.cli("layers", "set", "com.example.editor", "tv-number-3", "--action", COPY,
                                    "--name", "编辑器")
    assert code == 0 and result["created"]
    layer = controller.profile.app_layers[0]
    assert layer.name == "编辑器" and layer.overrides["tv-number-3"]["single"] == Action("common", target="copy")
    assert controller.editing_layer_id == ""

    exported = tmp_path / "scheme.json"
    code, result = main_program.cli("export", "scheme.json")             # relative to the CLI's directory
    assert code == 0 and exported.exists()
    profiles_seen = []
    controller.profiles_changed.connect(lambda: profiles_seen.append(True))
    code, result = main_program.cli("import", "scheme.json")
    assert code == 0 and len(controller.profiles) == 2 and profiles_seen
    assert controller.profile.name.endswith(" 2")

    first = controller.profiles[0]
    code, result = main_program.cli("profiles", "use", first.id)
    assert code == 0 and controller.profile is first
    code, listed = main_program.cli("remotes", "list")
    assert code == 0 and listed["remotes"][0]["id"] == controller.remote.id
    assert main_program.cli("remotes", "use", controller.remote.id)[0] == 0


def test_cli_for_another_config_dir_works_offline_even_while_the_app_runs(main_program, tmp_path_factory):
    other = tmp_path_factory.mktemp("other-config")
    code, result = main_program.cli("status", config_dir=other)
    assert code == 0 and result["mode"] == "offline"


def test_a_second_launch_still_just_shows_the_window(main_program):
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(main_program.name)
    assert socket.waitForConnected(1000)
    socket.write(b"show")
    socket.flush()
    socket.disconnectFromServer()
    main_program.qtbot.waitUntil(lambda: main_program.shown == 1)


def test_the_instance_channel_only_accepts_the_current_user(qapp, monkeypatch, private_channel):
    from PySide6.QtNetwork import QLocalServer
    from remotehub import app

    monkeypatch.setattr(app, "INSTANCE", private_channel)
    assert app.claim_instance(qapp) is False
    server = qapp.instance_server
    try:
        assert server.socketOptions() & QLocalServer.SocketOption.UserAccessOption
    finally:
        server.close()
        del qapp.instance_server


def test_cli_entry_point_runs_as_a_module(tmp_path, private_channel):
    env = {**os.environ, "PYTHONPATH": str(DESKTOP), "REMOTEHUB_CLI_INSTANCE": private_channel}
    done = subprocess.run([sys.executable, "-m", "remotehub.cli", "--config-dir", str(tmp_path), "status"],
                          env=env, capture_output=True, timeout=60)
    assert done.returncode == 0
    assert "当前遥控器" in done.stdout.decode("utf-8")
    done = subprocess.run([sys.executable, "-m", "remotehub.cli", "keys", "set"], env=env, capture_output=True, timeout=60)
    assert done.returncode == cli_service.USAGE and "错误：参数有误" in done.stderr.decode("utf-8")


def test_keys_list_shows_every_code_of_a_dual_mode_key(capsys, tmp_path):
    initialised(capsys, tmp_path)
    store = load(tmp_path)
    profile = next(p for p in store.remote.profiles if p.id == store.settings["active_profile_id"])
    key(profile, "tv-up").extra_triggers.append("hid:12:66")
    store.save()

    code, listed = run_json(capsys, tmp_path, "keys", "list")
    keys = {item["id"]: item for item in listed["keys"]}
    assert code == 0 and keys["tv-up"]["code"] == "hid:7:82"
    assert keys["tv-up"]["codes"] == ["hid:7:82", "hid:12:66"]


def test_piped_output_is_utf8_even_when_the_system_encoding_is_gbk(tmp_path, monkeypatch, private_channel):
    """Chinese Windows pipes default to GBK, which cannot encode 「音量 −」; frozen apps ignore PYTHONUTF8."""
    import io

    from remotehub import cli

    raw = io.BytesIO()
    piped = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", piped)
    monkeypatch.setenv("REMOTEHUB_CLI_INSTANCE", private_channel)
    code = cli.main(["actions", "kinds", "--json", "--config-dir", str(tmp_path)])
    piped.flush()
    assert code == 0
    text = raw.getvalue().decode("utf-8")
    assert "−" in text and json.loads(text)

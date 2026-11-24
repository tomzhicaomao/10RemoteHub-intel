"""Main-program side of the agent CLI: requests over the single-instance channel.

``app.py`` hands every new connection on its ``QLocalServer`` to :func:`accept`
first. A connection that starts with the CLI protocol line is answered here; any
other connection (a second launch asking to show the window) is left to the
existing handler. The server is created with ``UserAccessOption`` so only the
current user can connect.

Writes go through ``AppController`` public methods (``select_remote``,
``select_profile``, ``select``/``select_editing_layer``/``assign``,
``add_app_layer``, ``import_profile``) so the change is saved, takes effect at
once and the window refreshes. Before that, the change is planned on a copy with
the same rules as offline, which gives the same messages and powers ``--dry-run``.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import time
from pathlib import Path

from . import cli_service as service
from .cli_service import CliError

READ_WAIT_MS = 2000


def accept(socket, controller):
    """Answer ``socket`` if it carries a CLI request; False leaves it to the caller untouched."""
    magic = service.PROTOCOL
    deadline = time.monotonic() + .2
    while socket.bytesAvailable() < len(magic) and time.monotonic() < deadline:
        if not socket.waitForReadyRead(50) and not _connected(socket):
            break
    head = bytes(socket.peek(len(magic)))
    if head != magic:
        return False
    data = b""
    deadline = time.monotonic() + READ_WAIT_MS / 1000
    while b"\n" not in data[len(magic):] and len(data) <= service.MAX_REQUEST + len(magic):
        if socket.bytesAvailable():
            data += bytes(socket.readAll())
            continue
        if time.monotonic() >= deadline or (not socket.waitForReadyRead(100) and not _connected(socket)):
            break
    code, payload = handle(data[len(magic):].split(b"\n", 1)[0], controller)
    socket.write(json.dumps({"code": code, "payload": payload}, ensure_ascii=False).encode("utf-8") + b"\n")
    deadline = time.monotonic() + 10
    while socket.bytesToWrite() > 0 and time.monotonic() < deadline:   # an export carries the photo
        if not socket.waitForBytesWritten(500) and not _connected(socket):
            break
    socket.disconnectFromServer()
    socket.deleteLater()
    return True


def _connected(socket):
    from PySide6.QtNetwork import QLocalSocket

    return socket.state() == QLocalSocket.LocalSocketState.ConnectedState


def handle(line, controller):
    """Run one request line; returns (exit code, payload)."""
    try:
        request = json.loads(line.decode("utf-8"))
        argv = request["argv"]
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        return service.CHANNEL, CliError("命令行请求格式无效。", "请用同一版本的 10remotehub-cli。", service.CHANNEL).payload()
    requested = request.get("config_dir")
    if requested and Path(requested).resolve() != Path(controller.store.directory).resolve():
        # The CLI asked for another configuration directory: it edits that one offline.
        return service.OTHER_DIRECTORY, {"ok": False}
    try:
        args = service.parse(argv)
    except CliError as exc:
        return exc.code, exc.payload()
    return service.execute(args, OnlineSession(controller))


@contextmanager
def _messages(controller):
    seen = []

    def collect(text, error):
        seen.append((str(text), bool(error)))

    controller.message.connect(collect)
    try:
        yield seen
    finally:
        controller.message.disconnect(collect)


def _failure(seen, fallback):
    errors = [text for text, error in seen if error]
    return errors[-1] if errors else (getattr(fallback, "last_error", "") or "主程序拒绝了这次改动。")


class OnlineSession:
    """Carries out CLI commands inside the running main program."""

    mode = "online"

    def __init__(self, controller):
        self.controller = controller
        self.store = controller.store

    def status(self):
        view = self.controller.remote_view()
        return service.status_payload(
            self.store, connection={"state": view.title, "detail": view.detail, "tone": view.tone},
            input_path=self.controller.input_path())

    # -- planning on copies ------------------------------------------------------

    def _copies(self, remote, profile):
        twin = deepcopy(remote)
        return twin, next(p for p in twin.profiles if p.id == profile.id)

    def _validate(self, remote):
        try:
            remote.validate()
        except ValueError as exc:
            raise CliError(f"改动没有通过校验：{exc}", "按提示修改动作后重试；用 actions kinds 查看字段。")

    def _apply(self, call, plan):
        controller = self.controller
        with _messages(controller) as seen:
            done = call()
        if done is False or done is None:
            raise CliError(_failure(seen, controller), "按提示修改后重试。")
        return service.plan_payload(plan)

    # -- writes ------------------------------------------------------------------

    def commit_remote(self, target, plan, dry_run):
        if dry_run or not plan.changes:
            return service.plan_payload(plan)
        return self._apply(lambda: self.controller.select_remote(target.id), plan)

    def commit_profile(self, remote, target, plan, dry_run):
        if dry_run or not plan.changes:
            return service.plan_payload(plan)
        index = next(i for i, p in enumerate(self.controller.profiles) if p.id == target.id)
        return self._apply(lambda: self.controller.select_profile(index) or True, plan)

    def commit_key(self, remote, profile, button, gesture, action, dry_run):
        twin, twin_profile = self._copies(remote, profile)
        twin_button = next(b for b in twin_profile.buttons if b.id == button.id)
        plan = service.plan_key_action(twin, twin_profile, twin_button, gesture, action)
        self._validate(twin)
        if dry_run or not plan.changes:
            return service.plan_payload(plan)
        return self._with_selection(button.id, "", lambda: self.controller.assign(action, gesture), plan)

    def commit_layer(self, remote, profile, button, gesture, action, app, name, dry_run):
        twin, twin_profile = self._copies(remote, profile)
        twin_button = next(b for b in twin_profile.buttons if b.id == button.id)
        plan = service.plan_layer_action(twin, twin_profile, twin_button, gesture, action, app, name,
                                         clear=action is None)
        self._validate(twin)
        if dry_run or not plan.changes:
            return service.plan_payload(plan)
        controller = self.controller
        restore = (controller.selected_id, controller.editing_layer_id)
        layer = service.find_layer(profile, app)
        if layer is None:
            from .app_layers import LayerApp

            twin_layer = next(item for item in twin_profile.app_layers if item.id == plan.data["layer"])
            with _messages(controller) as seen:
                layer = controller.add_app_layer(LayerApp(twin_layer.app_id, twin_layer.name))
            if layer is None:
                raise CliError(_failure(seen, controller), "检查应用标识后重试。")
            plan.data["layer"] = layer.id
        if action is None:
            call = lambda: controller.assign(service.Action(), gesture)
        else:
            call = lambda: controller.assign(action, gesture, explicit_override=action.kind == "none")
        return self._with_selection(button.id, layer.id, call, plan, restore)

    def _with_selection(self, button_id, layer_id, call, plan, restore=None):
        """Point the controller at the key (and layer), apply, then restore what the user had selected."""
        controller = self.controller
        previous_key, previous_layer = restore or (controller.selected_id, controller.editing_layer_id)
        try:
            controller.select_editing_layer(layer_id)
            controller.select(button_id)
            if controller.selected_id != button_id:
                raise CliError("主程序无法选中这颗键。", "用 keys list 确认键的 id。", service.NOT_FOUND)
            return self._apply(call, plan)
        finally:
            if previous_key and any(b.id == previous_key for b in controller.profile.buttons):
                controller.select(previous_key)
            controller.select_editing_layer(previous_layer if any(
                item.id == previous_layer for item in controller.profile.app_layers) else "")

    def commit_import(self, remote, profile, path, replace, dry_run):
        twin, twin_profile = self._copies(remote, profile)
        plan, _ = service.plan_import(twin, twin.profiles, service.read_import(path), twin_profile, replace=replace)
        self._validate(twin)
        if dry_run:
            return service.plan_payload(plan)
        return self._apply(lambda: self.controller.import_profile(path, replace=replace), plan)

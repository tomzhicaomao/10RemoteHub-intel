from remotehub.win_audio_route import WindowsInputRoute


def test_close_restores_saved_roles_and_is_idempotent(qapp, monkeypatch):
    import remotehub.win_audio_route as module
    current = {0: 'cable', 1: 'cable', 2: 'original-communications'}
    writes = []
    monkeypatch.setattr(module, 'current_default', lambda capture, role: current[role])
    def write(endpoint, role):
        writes.append((role, endpoint))
        current[role] = endpoint
    monkeypatch.setattr(module, 'set_endpoint', write)
    route = WindowsInputRoute()
    route.saved = {0: 'original', 1: 'original', 2: 'original-communications'}
    route.targets = {0: 'cable', 1: 'cable', 2: 'original-communications'}
    route.state = 'acquired'
    route.close()
    route.close()
    assert writes == [(0, 'original'), (1, 'original')]
    assert route.state == 'idle' and route.saved == {}


import pytest
from remotehub import win_audio_route as win

@pytest.fixture
def routing(qapp, monkeypatch):
    current = {0:'mic-a', 1:'mic-b', 2:'mic-c'}
    monkeypatch.setattr(win, 'defaults', lambda capture:dict(current))
    monkeypatch.setattr(win, 'current_default', lambda capture, role:current[role])
    monkeypatch.setattr(win, 'set_endpoint', lambda endpoint, role:current.update({role:endpoint}))
    route = win.WindowsInputRoute(find=lambda:'cable')
    yield route, current
    route.close()


def test_com_failure_is_not_success(monkeypatch):
    monkeypatch.setattr(win, '_create', lambda *args:object())
    monkeypatch.setattr(win, '_method', lambda p,i,*args:lambda *a:-2147024891 if i == 13 else 0)
    with pytest.raises(OSError):
        win.set_endpoint('cable', 0)


def test_route_partial_failure_rolls_back_all_changed_roles(routing, monkeypatch):
    route, current = routing
    before = dict(current)
    def set_endpoint(endpoint, role):
        if endpoint == 'cable' and role == 1:
            raise OSError('simulated failure')
        current[role] = endpoint
    monkeypatch.setattr(win, 'set_endpoint', set_endpoint)
    signals=[]
    route.ready.connect(lambda:signals.append('ready'))
    route.failed.connect(lambda _:signals.append('failed'))
    route.acquire()
    assert signals == ['failed'] and current == before and not route.saved


def test_route_requires_readback_confirmation(routing, monkeypatch):
    route, current = routing
    monkeypatch.setattr(win, 'set_endpoint', lambda *args:None)
    ready=[]
    route.ready.connect(lambda:ready.append(True))
    route.acquire()
    assert not ready and route.state != 'acquired'


def test_route_preserves_manual_choice(routing):
    route, current = routing
    route.acquire()
    current[1]='manual-choice'
    route.release()
    assert current == {0:'mic-a', 1:'manual-choice', 2:'mic-c'}


def test_route_restore_failure_is_retryable(routing, monkeypatch):
    route, current = routing
    route.acquire()
    def write(endpoint, role):
        if role == 0:
            raise OSError('restore failed')
        current[role] = endpoint
    monkeypatch.setattr(win, 'set_endpoint', write)
    route.release()
    assert current == {0:'cable', 1:'mic-b', 2:'mic-c'}
    assert route.saved == {0:'mic-a'}
    monkeypatch.setattr(win, 'set_endpoint', lambda e,r:current.update({r:e}))
    route.close()
    assert current[0] == 'mic-a' and not route.saved


def test_route_unreadable_default_does_not_discard_restore_state(routing, monkeypatch):
    route, current = routing
    route.acquire()
    with monkeypatch.context() as patch:
        patch.setattr(win, 'current_default', lambda capture, role:'')
        route.release()
        assert route.saved == {0:'mic-a', 1:'mic-b', 2:'mic-c'}
    route.close()
    assert current == {0:'mic-a', 1:'mic-b', 2:'mic-c'}


def test_voice_session_acquire_failure_neither_double_restores_nor_double_fails(qapp, monkeypatch):
    from remotehub.session import VoiceSession
    from test_ui import FakeAudio, FakeKeys

    current = {0: 'mic-a', 1: 'mic-b', 2: 'mic-c'}
    monkeypatch.setattr(win, 'defaults', lambda capture: dict(current))
    monkeypatch.setattr(win, 'current_default', lambda capture, role: current[role])

    def set_endpoint(endpoint, role):
        if endpoint == 'cable' and role == 1:
            raise OSError('switch failed')
        if endpoint == 'mic-a' and role == 0:
            raise OSError('rollback failed')
        current[role] = endpoint

    monkeypatch.setattr(win, 'set_endpoint', set_endpoint)
    route = win.WindowsInputRoute(find=lambda: 'cable')
    restores = []
    original_restore = route._restore

    def counted_restore():
        restores.append(True)
        return original_restore()

    monkeypatch.setattr(route, '_restore', counted_restore)
    session = VoiceSession(FakeAudio(), FakeKeys(), route=route)
    failures = []
    session.failed.connect(failures.append)
    session.start('usb')
    assert len(restores) == 1
    assert len(failures) == 1
    assert 'switch failed' in failures[0] and 'rollback failed' in failures[0]
    assert session.owner is None and not session.ending
    assert route.saved == {0: 'mic-a'}


def test_win32_controller_reports_route_failure_only_once(qapp, tmp_path, monkeypatch):
    import sys
    from remotehub.controller import AppController

    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr('remotehub.controller.DeviceInput.start', lambda *a, **k: None)
    monkeypatch.setattr('remotehub.controller.DeviceInput.stop', lambda *a, **k: True)
    controller = AppController(tmp_path, auto_mapping=True)
    try:
        assert isinstance(controller.voice.route, win.WindowsInputRoute)
        controller.outputs = [('CABLE Input (2- VB-Audio Virtual Cable)', b'id-2')]
        controller.voice.output_id = b'id-2'
        assert controller._selected_output_description() == 'CABLE Input (2- VB-Audio Virtual Cable)'
        monkeypatch.setattr(controller.voice.route, 'find', lambda: '')
        errors = []
        controller.message.connect(lambda text, is_error: errors.append(text) if is_error else None)
        controller.voice.start('usb')
        assert len(errors) == 1
        assert 'VB-CABLE' in errors[0]
    finally:
        controller.shutdown()


def test_com_apartment_balances_init_and_current_default_frees_empty_buffer(monkeypatch):
    import ctypes
    from types import SimpleNamespace

    calls = []
    freed = []

    class FakeOle32:
        def __init__(self, hr=0):
            self.hr = hr
        def CoInitialize(self, reserved):
            calls.append(('init', self.hr))
            return self.hr
        def CoUninitialize(self):
            calls.append(('uninit', None))
        def CoTaskMemFree(self, ptr):
            freed.append(ctypes.cast(ptr, ctypes.c_void_p).value)

    for hr, expected_uninit in ((0, True), (1, True), (-2147417850, False)):
        calls.clear()
        monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(ole32=FakeOle32(hr)), raising=False)
        with win._com_apartment():
            pass
        assert calls == ([('init', hr), ('uninit', None)] if expected_uninit else [('init', hr)])

    buf = ctypes.create_unicode_buffer('')
    addr = ctypes.addressof(buf)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(ole32=FakeOle32(0)), raising=False)
    monkeypatch.setattr(win, '_create', lambda *args: object())

    def fake_method(pointer, index, *argtypes):
        if index == 4:
            def get_default(this, flow, role, out_dev):
                out_dev._obj.value = 0x1234
                return 0
            return get_default
        if index == 5:
            def get_id(this, out_str):
                ctypes.cast(ctypes.byref(out_str._obj), ctypes.POINTER(ctypes.c_void_p))[0] = addr
                return 0
            return get_id
        return lambda *a: 0

    monkeypatch.setattr(win, '_method', fake_method)
    assert win.current_default(True, 0) == ''
    assert freed == [addr]


def test_match_cable_input_pairs_numbered_and_named_cable_instances():
    inputs = [
        ('麦克风阵列 (Realtek Audio)', 'mic-realtek'),
        ('CABLE Output (VB-Audio Virtual Cable)', 'cable-1'),
        ('CABLE Output (2- VB-Audio Virtual Cable)', 'cable-2'),
        ('CABLE-B Output (VB-Audio Cable B)', 'cable-b'),
    ]
    assert win.match_cable_input(inputs, 'CABLE Input (2- VB-Audio Virtual Cable)') == 'cable-2'
    assert win.match_cable_input(inputs, '遥控器播放 (2- VB-Audio Virtual Cable)') == 'cable-2'
    assert win.match_cable_input(inputs, 'CABLE-B Input (VB-Audio Cable B)') == 'cable-b'
    assert win.match_cable_input(inputs, 'CABLE Input (VB-Audio Virtual Cable)') == 'cable-1'
    assert win.match_cable_input(inputs, '') == 'cable-1'

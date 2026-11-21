import argparse
import sys

INSTANCE = "org.remotehub.desktop.instance"


def _configure_windows_process():
    """Enable Per-Monitor V2 DPI awareness and explicit taskbar AppUserModelID."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = getattr(ctypes.windll, "user32", None)
        if user32 is not None:
            try:
                # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (DPI_AWARENESS_CONTEXT)-4
                if not user32.SetProcessDpiAwarenessContext(ctypes.c_ssize_t(-4)):
                    user32.SetProcessDPIAware()
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass
        shell32 = getattr(ctypes.windll, "shell32", None)
        if shell32 is not None:
            shell32.SetCurrentProcessExplicitAppUserModelID("org.remotehub.desktop")
    except Exception:
        pass


def claim_instance(app):
    """Return True if a primary instance is already running and received our show request.

    Two copies would both register Raw Input / USB capture and both try to claim
    the receiver's voice interface, so a second launch must hand over and exit.
    """
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE)
    if socket.waitForConnected(300):
        if sys.platform == "win32":
            try:
                import ctypes
                # ASFW_ANY (0xFFFFFFFF) grants the background primary instance
                # foreground activation rights from this newly-launched process.
                ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
            except Exception:
                pass
        socket.write(b"show")
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return True
    QLocalServer.removeServer(INSTANCE)  # stale endpoint after a crash
    server = QLocalServer(app)
    server.listen(INSTANCE)
    app.instance_server = server
    return False


def main():
    if "--usb-worker" in sys.argv:
        from .usb_worker import run
        run(sys.argv[sys.argv.index("--usb-worker") + 1:])
        return
    if sys.platform not in ("darwin", "win32") and not sys.platform.startswith("linux"):
        raise SystemExit("当前版本仅支持 macOS、Windows 与 Linux")
    p = argparse.ArgumentParser()
    p.add_argument("--config-dir")
    p.add_argument("--smoke", action="store_true", help="No devices, permissions or system settings are changed")
    p.add_argument("--screenshot")
    p.add_argument("--start-hidden", action="store_true", help="Start in the tray when it is available")
    p.add_argument("--permission-status", action="store_true", help="Print read-only permission status without UI or device access")
    p.add_argument("--diagnose", metavar="PATH", help="Write a read-only environment report and exit")
    args = p.parse_args()
    if args.diagnose:
        from .diagnose import write_report
        print(write_report(args.diagnose))
        return 0
    if args.permission_status:
        import json
        from .permissions import permission_status, running_app_bundle
        bundle = running_app_bundle()
        print(json.dumps({"bundle": str(bundle) if bundle else None,
                          "permissions": permission_status()}, ensure_ascii=False))
        return 0
    _configure_windows_process()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .branding import APP_NAME
    from .icons import configure_application
    from .ui import MainWindow
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("RemoteHub")
    app.setApplicationDisplayName(APP_NAME)
    configure_application(app, native=not args.smoke)
    if not args.smoke and claim_instance(app):
        print("10RemoteHub 已在运行，已切换到已打开的窗口。")
        return 0
    window = MainWindow(args.config_dir, safe=args.smoke)
    server = getattr(app, "instance_server", None)
    if server is not None and not args.smoke:
        def bring_forward():
            while server.hasPendingConnections():
                sock = server.nextPendingConnection()
                sock.waitForReadyRead(200)
                sock.close()
                window.restore_from_tray()
        server.newConnection.connect(bring_forward)
    app.setQuitOnLastWindowClosed(window.tray is None)
    if args.start_hidden and window.tray is not None and not args.smoke:
        window.hide_to_tray()
    else:
        window.show()
    if args.smoke:
        def finish():
            if args.screenshot:
                window.grab().save(args.screenshot)
            window.shutdown()
            app.quit()
        QTimer.singleShot(800, finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

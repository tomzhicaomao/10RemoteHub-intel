"""User feedback 2026-11-20: the My Remotes page and the four-corner picker."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage

from remotehub.model import Button
from remotehub.ui.photo_crop import PhotoCropCanvas, PhotoCropDialog
from remotehub.ui.window import MAPPING_HINT, MainWindow


def make_window(qtbot, tmp_path):
    window = MainWindow(tmp_path, safe=True)
    qtbot.addWidget(window)
    window.show()
    return window


def test_an_added_remote_shows_status_and_protocol_library(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)       # upgraded 0.2.0 config: the 0416:0602 remote
    page = window.remotes_page
    window.show_page(window.pages.index(page))
    remote = window.controller.remote
    assert not hasattr(page, "catalog_cards")
    assert page.catalog.isVisible() and page.protocol_card.isVisible()
    card = page.saved_cards[remote.id]
    assert card.badge.text() == "● 正在使用"
    assert card.status.text() in ("已连接", "未连接")


def test_remove_shows_clean_empty_state_without_unplugged_product_cards(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = make_window(qtbot, tmp_path)
    page = window.remotes_page
    window.show_page(window.pages.index(page))
    remote = window.controller.remote
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    page.remove_remote(remote.id)
    assert page.empty.isVisible()
    assert not hasattr(page, "catalog_cards")
    assert page.catalog.isVisible() and page.protocol_card.isVisible()


def test_mapping_hint_and_profiles_only_on_the_mapping_page(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    sidebar = window.sidebar
    window.show_page(window.pages.index(window.remotes_page))
    assert window.feedback.text() == ""
    assert not sidebar.profile_card.isVisible() and not sidebar.scheme_header.isVisible()
    assert not sidebar.add_remote_button.isVisible()     # the page has its own add button

    window.show_page(window.pages.index(window.mapping))
    assert window.feedback.text() == MAPPING_HINT
    assert sidebar.profile_card.isVisible() and sidebar.add_remote_button.isVisible()

    window.controller.tell("已保存")                       # a real message is never wiped by navigation
    window.show_page(window.pages.index(window.remotes_page))
    assert window.feedback.text() == "已保存"


def corner_canvas(qtbot):
    photo = QImage(400, 600, QImage.Format.Format_RGB32)
    photo.fill(Qt.GlobalColor.darkGray)
    dialog = PhotoCropDialog(photo, None)
    qtbot.addWidget(dialog)
    dialog.resize(760, 650)
    dialog.show()
    return dialog, dialog.canvas


def test_picking_corners_shows_a_pointing_hand_and_hands_on_corners(qtbot):
    dialog, canvas = corner_canvas(qtbot)
    inside = canvas.image_rect().center()
    assert canvas.cursor_shape_at(inside) == Qt.CursorShape.PointingHandCursor
    assert canvas.cursor_shape_at(QPointF(1, 1)) == Qt.CursorShape.ArrowCursor
    assert "左上角" in dialog.hint.text() and "1/4" in dialog.hint.text()

    rect = canvas.image_rect()
    for x, y in ((.1, .1), (.9, .1), (.9, .9), (.1, .9)):
        qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton,
                         pos=QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height()).toPoint())
    corner = canvas.display_point(canvas.corners[0])
    assert canvas.cursor_shape_at(corner) == Qt.CursorShape.OpenHandCursor
    canvas._dragging = True
    assert canvas.cursor_shape_at(corner) == Qt.CursorShape.ClosedHandCursor
    canvas._dragging = False
    assert canvas.cursor().shape() != Qt.CursorShape.CrossCursor

    dialog.crop_button.click()                            # preview: nothing to pick any more
    assert canvas.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert isinstance(canvas, PhotoCropCanvas)


def open_learning_dialog(qtbot, window):
    from remotehub.device_probe import DeviceProbeDialog
    from remotehub.probe_device import ProbeDevice

    device = ProbeDevice("bt-1", "遥控器", "蓝牙", 0x1234, 0x5678, "u1")
    dialog = DeviceProbeDialog(None, window, device=device, install=lambda remote, device: True)
    qtbot.addWidget(dialog)
    dialog.open()
    return dialog


def test_close_sits_beside_save_at_the_bottom(qtbot, tmp_path):
    window = make_window(qtbot, tmp_path)
    dialog = open_learning_dialog(qtbot, window)
    footer = dialog.footer.layout()
    widgets = [footer.itemAt(i).widget() for i in range(footer.count()) if footer.itemAt(i).widget()]
    assert widgets == [dialog.close_button, dialog.enrollment.save_button]
    dialog.enrollment.setEnabled(False)
    assert not dialog.enrollment.save_button.isEnabled() and dialog.close_button.isEnabled()
    dialog.photo.mark_clean()
    dialog.close()


def test_quit_closes_dialogs_and_asks_about_unsaved_keys(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = make_window(qtbot, tmp_path)
    quits = []
    monkeypatch.setattr(window, "quit_app", lambda: quits.append(True))
    dialog = open_learning_dialog(qtbot, window)
    dialog.photo.dirty = True
    dialog.photo.profile.buttons.append(Button(name="按键 1", trigger="hid:7:30"))

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)
    assert window.request_quit() is False                  # user keeps the dialog
    assert dialog.isVisible() and not quits

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)
    assert window.request_quit() is True
    qtbot.waitUntil(lambda: bool(quits))
    assert not dialog.isVisible()


def test_quit_handler_only_installs_on_the_real_cocoa_platform():
    from remotehub import mac_quit

    assert mac_quit.install(lambda: None) is False          # offscreen test platform

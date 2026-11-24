"""First-run guide: add a remote, prepare the system, then verify a key.

The preparation step is the shared setup checklist (``setup_checklist``): only what
the added remote needs on this platform. Adding a remote moves straight to it.
Shown once; the menu offers "重新运行新手引导…".
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout

from ..branding import APP_NAME
from ..controller import StatusView
from ..icons import app_icon
from ..theme import TOKENS, stylesheet
from .setup_checklist import ADD, GUIDE_STYLE, SETTINGS, VOICE, SetupChecklist
from .widgets import Pill, button, label

PAGES = 4


class OnboardingDialog(QDialog):
    open_mapping = Signal()
    open_settings = Signal()
    open_voice = Signal()
    add_remote = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.verified = False
        self.c.physical_key.connect(self.verify_key)
        self.c.remotes_changed.connect(self.refresh_remote)
        self.setWindowTitle(APP_NAME + " · 开始使用")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.setStyleSheet(stylesheet() + f"QDialog {{ background: {TOKENS['PANEL']}; }}" + GUIDE_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(14)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._welcome())
        self.stack.addWidget(self._add())
        self.stack.addWidget(self._prepare())
        self.stack.addWidget(self._done())
        outer.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        self.skip = button("跳过", self.finish)
        footer.addWidget(self.skip)
        footer.addStretch(1)
        self.back = button("上一步", self.back_page)
        self.next = button("下一步", self.next_page, role="primary")
        footer.addWidget(self.back)
        footer.addWidget(self.next)
        outer.addLayout(footer)
        self.show_page(0)

    # -- pages -----------------------------------------------------------------

    def _page(self, title, body, *widgets, icon=None):
        page = QFrame()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        if icon is not None:
            box.addWidget(icon)
            box.addSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("guideTitle")
        heading.setWordWrap(True)
        box.addWidget(heading)
        for text in body:
            paragraph = QLabel(text)
            paragraph.setObjectName("guideBody")
            paragraph.setWordWrap(True)
            box.addWidget(paragraph)
        for widget in widgets:
            box.addWidget(widget)
        box.addStretch(1)
        return page

    def _welcome(self):
        steps = QFrame()
        steps.setObjectName("guideCard")
        box = QVBoxLayout(steps)
        box.setContentsMargins(18, 14, 18, 14)
        box.setSpacing(8)
        for number, text in (
            ("1", "插入接收器或连接蓝牙，添加你的遥控器"),
            ("2", "在遥控器照片上给每个键分配动作"),
            ("3", "想用遥控器说话时，再打开语音"),
        ):
            line = QLabel(f"{number}   {text}")
            line.setObjectName("guideBody")
            box.addWidget(line)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(72, 72))
        return self._page(
            "把遥控器变成你的快捷键面板",
            ["按键怎么用由你决定；说话交给输入法或语音识别软件。按键和语音内容不上传，仅更新检查和下载会联网。"],
            steps,
            icon=icon,
        )

    def _go_settings(self):
        self.finish()
        self.open_settings.emit()

    def _go_voice(self):
        self.finish()
        self.open_voice.emit()

    def _add(self):
        self._had_remote = self.c.remote is not None
        self.remote_pill = Pill("未添加")
        self.remote_detail = label("", "caption")
        card = QFrame()
        card.setObjectName("guideCard")
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 14, 18, 14)
        box.setSpacing(8)
        line = QHBoxLayout()
        heading = QLabel("遥控器")
        heading.setObjectName("guideStep")
        line.addWidget(heading, 1)
        line.addWidget(self.remote_pill)
        line.addWidget(button("添加遥控器", self.add_remote.emit, role="primary"))
        box.addLayout(line)
        box.addWidget(self.remote_detail)
        page = self._page(
            "第一步：添加遥控器",
            ["插入遥控器的 USB 接收器，或先在系统设置里连接蓝牙遥控器。在添加窗口里选中它，按一下遥控器上的键再松开。",
             "然后导入一张遥控器正面照片：有麦克风的先在照片上标出语音键，再逐个点按键位置并按实体键。"
             "同一种接收器插哪一个都能用。"],
            card,
        )
        self.refresh_remote()
        return page

    def refresh_remote(self):
        if not hasattr(self, "remote_pill"):
            return
        remote = self.c.remote
        if remote is None:
            self.remote_pill.show_status(StatusView("未添加", "", "neutral"))
            self.remote_detail.setText("添加后可以随时在「我的遥控器」里再添加、切换或删除。")
        else:
            self.remote_pill.show_status(StatusView("已添加", "", "ok"))
            self.remote_detail.setText(f"当前遥控器：{remote.name}")
        added = remote is not None and not getattr(self, "_had_remote", True)
        self._had_remote = remote is not None
        if added and getattr(self, "index", 0) == 1:
            # Straight on to what this remote still needs; let the add dialog close first.
            QTimer.singleShot(0, lambda: self.show_page(2))

    def _prepare(self):
        self.checklist = SetupChecklist(self.c)
        self.checklist.navigate.connect(self._navigate)
        self.prepare_lead = QLabel("")
        self.prepare_lead.setObjectName("guideBody")
        self.prepare_lead.setWordWrap(True)
        return self._page("第二步：让电脑能读取遥控器", [], self.prepare_lead, self.checklist)

    def _navigate(self, page):
        if page == SETTINGS:
            self._go_settings()
        elif page == VOICE:
            self._go_voice()
        elif page == ADD:
            self.add_remote.emit()

    def _done(self):
        self.verify_pill = Pill("等待按键")
        self.verify_detail = label("按键不需要先设置动作；这里只确认电脑能收到遥控器信号。", "caption")
        return self._page(
            "第三步：验证遥控器按键",
            ["现在按一下遥控器上任意一个已学会的键。收到信号后，点击「完成」进入按键映射；也可以跳过。"
             "之后点遥控器照片上的按键，就能分配快捷键、应用、文件或文本。"],
            self.verify_pill,
            self.verify_detail,
        )

    def verify_key(self, trigger, down):
        if self.index != PAGES - 1 or not down or not any(
            b.configurable and b.matches_trigger(trigger) for b in self.c.profile.buttons
        ):
            return
        self.verified = True
        self.verify_pill.show_status(StatusView("按键已验证", "", "ok"))
        name = next((b.name for b in self.c.profile.buttons if b.matches_trigger(trigger)), "")
        self.verify_detail.setText(f"已收到「{name}」的真实按键信号，遥控器读取链路正常。")

    # -- flow ------------------------------------------------------------------

    def show_page(self, index):
        self.index = max(0, min(PAGES - 1, index))
        self.stack.setCurrentIndex(self.index)
        self.back.setVisible(self.index > 0)
        self.next.setText("完成" if self.index == PAGES - 1 else "下一步")
        if self.index == 1:
            self.refresh_remote()
        elif self.index == 2:
            self.refresh()

    def next_page(self):
        if self.index == PAGES - 1:
            self.open_mapping.emit()
            self.finish()
            return
        self.show_page(self.index + 1)

    def back_page(self):
        self.show_page(self.index - 1)

    def refresh(self):
        if not self.c.has_remote:
            lead = "还没有添加遥控器。可以先回到上一步添加，也可以稍后在「我的遥控器」里添加。"
        else:
            lead = "下面是这只遥控器还需要的准备：「必须完成」的做完才能用按键，「可选」的需要时再做。做完会自动打勾。"
        self.prepare_lead.setText(lead)
        self.checklist.refresh()

    def finish(self):
        self.c.finish_onboarding()
        self.accept()

"""Fast VK Video Client & Downloader — main window."""
from __future__ import annotations

import os
import sys
import time
import uuid
from collections import deque

from PyQt6.QtCore import QByteArray, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QDesktopServices, QFont, QGuiApplication, QImage, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout, QMessageBox,
    QLineEdit, QMainWindow, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QStatusBar, QToolTip, QVBoxLayout, QWidget,
)

from config import Config
from player_widget import CinemaOverlay
from vk_search import (QUALITY_OPTIONS, SORT_OPTIONS, ChannelInfo, DownloadWorker, SearchWorker,
                       ThumbnailLoader, VideoItem, playback_format, remember_channel)

CARD_W, THUMB_H = 320, 180

STYLE = """
* { font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px; color: #e6e8ee; }
QMainWindow, #central { background: #0e0f13; }
#topBar, #pathBar { background: #15171d; border-bottom: 1px solid #22252e; }
QLineEdit, QComboBox {
    background: #1d2028; border: 1px solid #2a2e39; border-radius: 6px; padding: 7px 10px;
    selection-background-color: #3b6cff;
}
QLineEdit:focus, QComboBox:focus { border-color: #3b6cff; }
QLineEdit[readOnly="true"] { color: #a9afbf; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #1d2028; border: 1px solid #2a2e39; selection-background-color: #2b3a66; }
QPushButton {
    background: #252a35; border: 1px solid #2f3442; border-radius: 6px; padding: 7px 14px;
}
QPushButton:hover { background: #2e3442; }
QPushButton:pressed { background: #222632; }
QPushButton:disabled { color: #6b7080; }
QPushButton#primary { background: #3b6cff; border-color: #3b6cff; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #5280ff; }
QScrollArea, #grid { background: #0e0f13; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2c303c; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3a3f4e; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#card { background: #161920; border: 1px solid #20232c; border-radius: 10px; }
#card:hover { border-color: #33394a; }
#card[selected="true"] { border: 1px solid #3b6cff; background: #18203a; }
#batchBar { background: #172038; border-top: 1px solid #2b3a66; }
#batchBar QLabel { color: #dfe6ff; font-weight: 600; }
#cardTitle { font-size: 13px; font-weight: 600; color: #f1f3f8; }
#cardTitle:hover { color: #7fa0ff; }
#cardMeta { color: #8b91a3; font-size: 12px; }
#cardChannel { color: #9aa6c8; font-size: 12px; }
#cardChannel:hover { color: #7fa0ff; text-decoration: underline; }
#cardWarn { color: #e5a84d; font-size: 11px; }
QPushButton#dlBtn { padding: 5px 12px; font-size: 12px; }
QStatusBar { background: #15171d; border-top: 1px solid #22252e; }
QStatusBar QLabel { color: #9aa1b3; }
QProgressBar { background: #1d2028; border: 1px solid #2a2e39; border-radius: 5px; height: 12px;
    text-align: center; font-size: 11px; }
QProgressBar::chunk { background: #3b6cff; border-radius: 4px; }
#emptyLabel { color: #6f7586; font-size: 15px; }
"""


# ---------------------------------------------------------------- widgets ---
class FlowLayout(QLayout):
    """Wrapping layout so the card grid reflows with the window width."""

    def __init__(self, parent=None, spacing=16):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize(CARD_W, 0)
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        if not self._items:
            return m.top() + m.bottom()
        item_w = self._items[0].sizeHint().width()
        cols = max(1, (eff.width() + self._spacing) // (item_w + self._spacing))
        used = cols * item_w + (cols - 1) * self._spacing
        x0 = eff.x() + max(0, (eff.width() - used) // 2)   # center the grid
        y = eff.y()
        row_h = 0
        for i, item in enumerate(self._items):
            col = i % cols
            if col == 0 and i:
                y += row_h + self._spacing
                row_h = 0
            hint = item.sizeHint()
            if not test_only:
                item.setGeometry(QRect(QPoint(x0 + col * (item_w + self._spacing), y), hint))
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y() + m.bottom()


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


class ThumbCheckBox(QCheckBox):
    """Selection box overlaid on a thumbnail. Painted by hand (rounded box +
    checkmark) so it reads on any thumbnail; the whole 26 px square is the hit
    area. It consumes its own clicks, so the thumbnail's play action never
    fires when toggling selection."""

    SIZE = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Select for batch download")

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(2, 2, -2, -2)
        hover = self.underMouse()
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#3b6cff"))
            p.drawRoundedRect(r, 6, 6)
            path = QPainterPath()
            path.moveTo(r.left() + r.width() * 0.25, r.top() + r.height() * 0.52)
            path.lineTo(r.left() + r.width() * 0.43, r.top() + r.height() * 0.70)
            path.lineTo(r.left() + r.width() * 0.76, r.top() + r.height() * 0.32)
            p.setPen(QPen(Qt.GlobalColor.white, 2.4, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        else:
            p.setPen(QPen(QColor("#7fa0ff" if hover else "#f0f2f8"), 2))
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(r, 6, 6)

    def enterEvent(self, ev):
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.update()
        super().leaveEvent(ev)


class VideoCard(QFrame):
    playRequested = pyqtSignal(object)
    downloadRequested = pyqtSignal(object)
    channelRequested = pyqtSignal(object)
    selectionToggled = pyqtSignal(object, bool)

    def __init__(self, item: VideoItem, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("card")
        self.setFixedWidth(CARD_W + 2)

        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 10)
        v.setSpacing(6)

        self.thumb = ClickableLabel(self)
        self.thumb.setFixedSize(CARD_W, THUMB_H)
        self.thumb.setStyleSheet("background: #0b0c10; border-top-left-radius: 9px; border-top-right-radius: 9px;")
        self.thumb.clicked.connect(lambda: self.playRequested.emit(self.item))
        self._set_thumb(None)
        v.addWidget(self.thumb)
        self.check = ThumbCheckBox(self.thumb)
        self.check.move(8, 8)
        self.check.toggled.connect(self._on_check_toggled)

        body = QVBoxLayout()
        body.setContentsMargins(10, 0, 10, 0)
        body.setSpacing(3)
        self.title = ClickableLabel("", self)
        self.title.setObjectName("cardTitle")
        self.title.setToolTip(item.title)
        self.title.ensurePolished()   # apply the bold stylesheet font before measuring
        fm = self.title.fontMetrics()
        self.title.setText(_elide_lines(item.title, fm, CARD_W - 20, 2))
        self.title.setFixedHeight(fm.lineSpacing() * 2 + 2)
        self.title.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.title.clicked.connect(lambda: self.playRequested.emit(self.item))
        body.addWidget(self.title)

        channel = ClickableLabel(item.channel, self)
        channel.setObjectName("cardChannel")
        channel.setToolTip("Filter by this channel (copies it to the clipboard)"
                           + (f"\n@{item.channel_handle}" if item.channel_handle else ""))
        channel.clicked.connect(lambda: self.channelRequested.emit(self.item))
        body.addWidget(channel, 0, Qt.AlignmentFlag.AlignLeft)

        row = QHBoxLayout()
        meta = QLabel(f"{item.views_text}  ·  {_ago(item.date)}", self)
        meta.setObjectName("cardMeta")
        row.addWidget(meta, 1)
        self.dl_btn = QPushButton("⬇ Download", self)
        self.dl_btn.setObjectName("dlBtn")
        self.dl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dl_btn.clicked.connect(lambda: self.downloadRequested.emit(self.item))
        row.addWidget(self.dl_btn)
        body.addLayout(row)

        if not item.playable:
            warn = QLabel(f"⚠ {item.restriction or 'Playback restricted'}", self)
            warn.setObjectName("cardWarn")
            body.addWidget(warn)
        v.addLayout(body)

    def _set_thumb(self, image: QImage | None):
        pm = QPixmap(CARD_W, THUMB_H)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if image is not None:
            p.drawImage(0, 0, image)
        else:
            p.fillRect(pm.rect(), Qt.GlobalColor.black)
        # Duration badge
        text = self.item.duration_text
        f = QFont(self.font())
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(text) + 12
        badge = QRect(CARD_W - tw - 8, THUMB_H - 26, tw, 20)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(Qt.GlobalColor.black)
        p.setOpacity(0.75)
        p.drawRoundedRect(badge, 4, 4)
        p.setOpacity(1.0)
        p.setPen(Qt.GlobalColor.white)
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        self.thumb.setPixmap(pm)

    def set_thumbnail(self, image: QImage):
        self._set_thumb(image)

    def _on_check_toggled(self, checked: bool):
        self._show_selected(checked)
        self.selectionToggled.emit(self.item, checked)

    def _show_selected(self, checked: bool):
        self.setProperty("selected", checked)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_selected(self, checked: bool):
        """Programmatic (de)select without emitting selectionToggled."""
        self.check.blockSignals(True)
        self.check.setChecked(checked)
        self.check.blockSignals(False)
        self._show_selected(checked)

    DL_STATES = {
        "idle": ("⬇ Download", True),
        "queued": ("Queued…", False),
        "active": ("Downloading…", False),
        "done": ("✓ Downloaded", True),
        "failed": ("⚠ Retry", True),
    }

    def set_download_state(self, state: str):
        text, enabled = self.DL_STATES[state]
        self.dl_btn.setText(text)
        self.dl_btn.setEnabled(enabled)

    def set_downloading(self, active: bool):
        self.set_download_state("active" if active else "idle")


class BatchBar(QFrame):
    """Shown while >= 1 video is selected."""

    downloadRequested = pyqtSignal()
    selectAllRequested = pyqtSignal()
    clearRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("batchBar")
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(10)
        self.label = QLabel("", self)
        h.addWidget(self.label, 1)
        self.select_all = QPushButton("Select All on Page", self)
        self.select_all.clicked.connect(self.selectAllRequested)
        self.clear = QPushButton("Deselect All", self)
        self.clear.clicked.connect(self.clearRequested)
        self.download = QPushButton("", self)
        self.download.setObjectName("primary")
        self.download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download.clicked.connect(self.downloadRequested)
        for b in (self.select_all, self.clear, self.download):
            h.addWidget(b)
        self.hide()

    def set_count(self, n: int, off_page: int = 0):
        self.setVisible(n > 0)
        text = f"{n} video{'s' if n != 1 else ''} selected"
        if off_page:
            text += f"  ·  {off_page} from earlier searches"
        self.label.setText(text)
        self.download.setText(f"⬇ Download Selected ({n})")


class DownloadBar(QWidget):
    """Compact download status for the bottom status bar."""

    cancelRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.label = QLabel("", self)
        self.label.setMinimumWidth(200)
        self.label.setMaximumWidth(420)
        self.bar = QProgressBar(self)
        self.bar.setFixedWidth(220)
        self.bar.setFixedHeight(16)
        self.bar.setRange(0, 1000)
        self.speed = QLabel("", self)
        self.speed.setMinimumWidth(230)
        self.cancel = QPushButton("Cancel", self)
        self.cancel.setFixedHeight(22)
        self.cancel.setStyleSheet("padding: 1px 10px; font-size: 11px;")
        self.cancel.clicked.connect(self.cancelRequested)
        for w in (self.label, self.bar, self.speed, self.cancel):
            h.addWidget(w)
        self.setVisible(False)

    def show_progress(self, text: str, pct: float, speed: str, eta: str, suffix: str = ""):
        self.setVisible(True)
        self.label.setText(self.label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, 400))
        self.label.setToolTip(text)
        if pct < 0:
            self.bar.setRange(0, 0)   # indeterminate
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(pct * 10))
            self.bar.setFormat(f"{pct:.1f}%")
        self.speed.setText(" · ".join(x for x in (speed, f"ETA {eta}" if eta else "", suffix) if x))


# ------------------------------------------------------------ main window ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = Config()
        self.setWindowTitle("VK Video — Fast Client & Downloader")
        self.resize(1360, 860)
        geo = self.cfg.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))

        self.generation = 0
        self.cards: dict[str, VideoCard] = {}
        self.cursor = None
        self.loading = False
        self.search_worker: SearchWorker | None = None
        self.downloads: dict[str, dict] = {}   # the active job (at most one)
        self.queue: deque[dict] = deque()       # FIFO of {"item", "out_dir", "quality"}
        self.selected: dict[str, VideoItem] = {}
        self.batch_done = 0                     # finished (ok/failed/skipped) in this run

        self.thumbs = ThumbnailLoader((CARD_W, THUMB_H))
        self.thumbs.loaded.connect(self._on_thumb)
        self.thumbs.start()

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_top_bar())
        root.addWidget(self._build_path_bar())

        # Results area (the cinema overlay covers exactly this widget).
        self.results_host = QWidget()
        rh = QVBoxLayout(self.results_host)
        rh.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self.results_host)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_widget = QWidget()
        self.grid_widget.setObjectName("grid")
        gv = QVBoxLayout(self.grid_widget)
        gv.setContentsMargins(20, 20, 20, 20)
        gv.setSpacing(16)
        self.empty_label = QLabel("Search VK Video to get started.", self.grid_widget)
        self.empty_label.setObjectName("emptyLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gv.addWidget(self.empty_label)
        self.flow_host = QWidget(self.grid_widget)
        self.flow = FlowLayout(self.flow_host)
        self.flow.setContentsMargins(0, 0, 0, 0)
        gv.addWidget(self.flow_host)
        self.more_btn = QPushButton("Load more", self.grid_widget)
        self.more_btn.clicked.connect(self.load_more)
        self.more_btn.hide()
        gv.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        gv.addStretch(1)
        self.scroll.setWidget(self.grid_widget)
        self.scroll.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        rh.addWidget(self.scroll)
        root.addWidget(self.results_host, 1)

        self.batch_bar = BatchBar()
        self.batch_bar.downloadRequested.connect(self.download_selected)
        self.batch_bar.selectAllRequested.connect(self.select_all_on_page)
        self.batch_bar.clearRequested.connect(self.deselect_all)
        root.addWidget(self.batch_bar)

        self.cinema = CinemaOverlay(self.results_host)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_label = QLabel("Ready")
        sb.addWidget(self.status_label, 1)
        self.dl_bar = DownloadBar()
        self.dl_bar.cancelRequested.connect(self.cancel_downloads)
        sb.addPermanentWidget(self.dl_bar)
        self._idle_timer = QTimer(self, singleShot=True, interval=6000)
        self._idle_timer.timeout.connect(
            lambda: self.dl_bar.setVisible(bool(self.downloads or self.queue)))

        aria = "aria2c ✓" if DownloadWorker.aria2c_available() else "aria2c not found (using built-in downloader)"
        self.status_label.setText(f"Ready · {aria}")
        self.search_edit.setFocus()

    # -- construction --------------------------------------------------------
    def _build_top_bar(self):
        bar = QWidget()
        bar.setObjectName("topBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(10)
        logo = QLabel("▶ VK Video")
        logo.setStyleSheet("font-size: 16px; font-weight: 700; color: #7fa0ff; padding-right: 8px;")
        h.addWidget(logo)

        self.search_edit = QLineEdit(self.cfg.get("last_query", ""))
        self.search_edit.setPlaceholderText("Search VK Video…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self.start_search)
        h.addWidget(self.search_edit, 3)

        self.sort_box = QComboBox()
        for label, *_ in SORT_OPTIONS:
            self.sort_box.addItem(label)
        self.sort_box.setCurrentIndex(min(int(self.cfg.get("sort", 0) or 0), len(SORT_OPTIONS) - 1))
        self.sort_box.setToolTip("Sort order. “Date: oldest” reverses the newest results that were loaded.")
        self.sort_box.currentIndexChanged.connect(self._on_sort_changed)
        h.addWidget(self.sort_box)

        self.channel_edit = QLineEdit(self.cfg.get("last_channel", ""))
        self.channel_edit.setPlaceholderText("Channel (@handle, URL or name)")
        self.channel_edit.textChanged.connect(self._update_search_placeholder)
        self._update_search_placeholder(self.channel_edit.text())
        self.channel_edit.setClearButtonEnabled(True)
        self.channel_edit.returnPressed.connect(self.start_search)
        h.addWidget(self.channel_edit, 1)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("primary")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.clicked.connect(self.start_search)
        h.addWidget(self.search_btn)
        return bar

    def _build_path_bar(self):
        bar = QWidget()
        bar.setObjectName("pathBar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(10)
        h.addWidget(QLabel("Save to:"))
        self.path_edit = QLineEdit(self.cfg.get("download_dir"))
        self.path_edit.setReadOnly(True)
        h.addWidget(self.path_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_dir)
        h.addWidget(browse)
        h.addSpacing(12)
        h.addWidget(QLabel("Quality:"))
        self.quality_box = QComboBox()
        for label, *_ in QUALITY_OPTIONS:
            self.quality_box.addItem(label)
        self.quality_box.setCurrentIndex(min(int(self.cfg.get("quality", 0) or 0), len(QUALITY_OPTIONS) - 1))
        self.quality_box.setToolTip("Download quality (playback is capped to this height too)")
        self.quality_box.currentIndexChanged.connect(lambda i: self.cfg.set("quality", i))
        h.addWidget(self.quality_box)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.path_edit.text())))
        h.addWidget(open_btn)
        return bar

    # -- download folder -----------------------------------------------------
    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose download folder", self.path_edit.text())
        if d:
            d = os.path.normpath(d)
            self.path_edit.setText(d)
            self.cfg.set("download_dir", d)

    # -- search --------------------------------------------------------------
    def _update_search_placeholder(self, channel: str = ""):
        self.search_edit.setPlaceholderText(
            "Search within this channel… (empty = all its videos)" if channel.strip()
            else "Search VK Video…")

    def _on_sort_changed(self, idx):
        self.cfg.set("sort", idx)
        if self.search_edit.text().strip():
            self.start_search()

    def start_search(self):
        query = self.search_edit.text().strip()
        channel = self.channel_edit.text().strip()
        if not query and not channel:
            self.search_edit.setFocus()
            return
        self.cfg.data.update(last_query=query, last_channel=channel)
        self.cfg.save()

        self.cinema.close_player()
        self.generation += 1
        self.thumbs.reset()
        self._clear_results()
        self.cursor = None
        self.channel_info = None
        self.empty_label.setText(f"Loading {channel}…" if channel and not query else "Searching…")
        self.empty_label.show()
        self._run_search(cursor=None)

    def load_more(self):
        if self.cursor and not self.loading:
            self._run_search(self.cursor)

    def _maybe_load_more(self, value):
        sbar = self.scroll.verticalScrollBar()
        if self.cursor and not self.loading and value >= sbar.maximum() - 400:
            self.load_more()

    def _run_search(self, cursor):
        self.loading = True
        self.search_btn.setEnabled(False)
        self.more_btn.setEnabled(False)
        self.more_btn.setText("Loading…")
        self.status_label.setText("Searching…")
        w = SearchWorker(self.generation, self.search_edit.text().strip(), self.sort_box.currentIndex(),
                         self.channel_edit.text(), cursor=cursor, parent=self)
        w.results.connect(self._on_results)
        w.channelInfo.connect(self._on_channel_info)
        w.failed.connect(self._on_search_failed)
        w.finished.connect(w.deleteLater)
        self.search_worker = w
        w.start()

    def _on_channel_info(self, gen, info):
        if gen == self.generation:
            self.channel_info = info

    def _on_results(self, gen, items, cursor):
        if gen != self.generation:
            return
        self.loading = False
        self.search_btn.setEnabled(True)
        self.cursor = cursor
        added = 0
        for it in items:
            if it.vid in self.cards:
                continue
            card = VideoCard(it, self.flow_host)
            card.playRequested.connect(self.play)
            card.downloadRequested.connect(self.download)
            card.channelRequested.connect(self.filter_by_channel)
            card.selectionToggled.connect(self._on_card_selected)
            if it.vid in self.selected:
                card.set_selected(True)
            state = self._download_state(it.vid)
            if state != "idle":
                card.set_download_state(state)
            self.flow.addWidget(card)
            self.cards[it.vid] = card
            self.thumbs.request(self.thumbs.generation, it.vid, it.thumb_url)
            added += 1
        self.flow_host.updateGeometry()
        ci = getattr(self, "channel_info", None)
        end = "" if cursor else " · end of results"
        if ci:
            ch: ChannelInfo = ci["channel"]
            if ci["query"]:
                summary = (f"{ch.label}: {len(self.cards)} videos matching “{ci['query']}” · "
                           f"searched {ci['scanned_to']:,} of {ci['total']:,} uploads")
                empty = (f"No videos in {ch.name} match “{ci['query']}”"
                         + (f" (searched {ci['scanned_to']:,} of {ci['total']:,} uploads so far)."
                            if cursor else f" (searched all {ci['total']:,} uploads)."))
            else:
                summary = f"{ch.label}: {len(self.cards)} of {ci['total']:,} videos"
                empty = f"{ch.name} has no public videos."
            self.status_label.setText(summary + end)
        else:
            self.status_label.setText(f"{len(self.cards)} results{end}")
            empty = "No results."
        self.empty_label.setVisible(not self.cards)
        if not self.cards:
            self.empty_label.setText(empty)
        self.more_btn.setVisible(cursor is not None)
        self.more_btn.setEnabled(True)
        self.more_btn.setText("Load more")
        # In-channel keyword search: keep scanning (up to 20k uploads) until a
        # handful of matches fill the view; beyond that "Load more" continues.
        if cursor and added == 0 and len(self.cards) < 8 and ci and ci["scanned_to"] < 20000:
            QTimer.singleShot(0, self.load_more)

    def _on_search_failed(self, gen, msg):
        if gen != self.generation:
            return
        self.loading = False
        self.search_btn.setEnabled(True)
        self.more_btn.setEnabled(True)
        self.more_btn.setText("Load more")
        self.status_label.setText(f"Search failed: {msg}")
        if not self.cards:
            self.empty_label.setText(f"Search failed:\n{msg}")
            self.empty_label.show()

    def _clear_results(self):
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        self.cards.clear()
        self.more_btn.hide()
        self.scroll.verticalScrollBar().setValue(0)

    def _on_thumb(self, gen, key, image):
        card = self.cards.get(key)
        if card is not None:
            card.set_thumbnail(image)

    # -- playback ------------------------------------------------------------
    def play(self, item: VideoItem):
        _, _, max_h = QUALITY_OPTIONS[self.quality_box.currentIndex()]
        self.cinema.open(item.url, item.title, playback_format(max_h))

    # -- channel filter ------------------------------------------------------
    def filter_by_channel(self, item: VideoItem):
        value = f"@{item.channel_handle}" if item.channel_handle else item.channel
        owner = int(item.vid.rsplit("_", 1)[0])
        remember_channel(value, ChannelInfo(owner, item.channel, item.channel_handle))  # skip lookup
        QGuiApplication.clipboard().setText(value)
        self.channel_edit.setText(value)
        self.start_search()
        note = f"Copied channel to clipboard and applied filter: {value}"
        self.status_label.setText(note)
        QToolTip.showText(QCursor.pos(), "Copied channel to clipboard and applied filter", self, QRect(), 2500)

    # -- selection -----------------------------------------------------------
    def _on_card_selected(self, item: VideoItem, checked: bool):
        if checked:
            self.selected[item.vid] = item
        else:
            self.selected.pop(item.vid, None)
        self._update_batch_bar()

    def _update_batch_bar(self):
        off_page = sum(1 for vid in self.selected if vid not in self.cards)
        self.batch_bar.set_count(len(self.selected), off_page)

    def select_all_on_page(self):
        for vid, card in self.cards.items():
            self.selected[vid] = card.item
            card.set_selected(True)
        self._update_batch_bar()

    def deselect_all(self):
        for vid in list(self.selected):
            if vid in self.cards:
                self.cards[vid].set_selected(False)
        self.selected.clear()
        self._update_batch_bar()

    def download_selected(self):
        items = list(self.selected.values())
        self.deselect_all()
        added = self.enqueue(items)
        skipped = len(items) - added
        if skipped:
            self.status_label.setText(f"Queued {added} video(s); {skipped} already queued/downloading.")

    # -- download queue ------------------------------------------------------
    # One download at a time (no bandwidth splitting, no VK rate-limit bursts);
    # the rest wait in FIFO order and start as soon as the active one ends.
    def _download_state(self, vid: str) -> str:
        if any(d["item"].vid == vid for d in self.downloads.values()):
            return "active"
        if any(q["item"].vid == vid for q in self.queue):
            return "queued"
        return "idle"

    def _set_card_state(self, vid: str, state: str):
        if vid in self.cards:
            self.cards[vid].set_download_state(state)

    def download(self, item: VideoItem):
        self.enqueue([item])

    def enqueue(self, items: list[VideoItem]) -> int:
        out_dir = self.path_edit.text()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self.status_label.setText(f"Cannot use download folder: {e}")
            return 0
        quality = self.quality_box.currentIndex()
        added = 0
        for item in items:
            if self._download_state(item.vid) != "idle":
                continue        # never the same video twice -> no file collisions
            self.queue.append({"item": item, "out_dir": out_dir, "quality": quality})
            self._set_card_state(item.vid, "queued")
            added += 1
        self._idle_timer.stop()
        self._pump()
        self._refresh_dl_bar()
        return added

    def _pump(self):
        """Start the next queued download if nothing is active."""
        if self.downloads or not self.queue:
            return
        q = self.queue.popleft()
        item = q["item"]
        job = uuid.uuid4().hex
        w = DownloadWorker(job, item.url, item.title, q["out_dir"],
                           quality_index=q["quality"], parent=self)
        w.progress.connect(self._on_dl_progress)
        w.status.connect(lambda j, msg: self._on_dl_progress(j, -1, msg, ""))
        w.finished_ok.connect(self._on_dl_done)
        w.failed.connect(self._on_dl_failed)
        w.finished.connect(w.deleteLater)
        self.downloads[job] = {"item": item, "worker": w, "pct": -1.0, "speed": "Starting…",
                               "eta": "", "t": time.monotonic()}
        self._set_card_state(item.vid, "active")
        self._refresh_dl_bar()
        w.start()

    def _refresh_dl_bar(self):
        d = next(iter(self.downloads.values()), None)
        if d is None:
            return
        total = self.batch_done + 1 + len(self.queue)
        prefix = f"Downloading ({self.batch_done + 1}/{total}): " if total > 1 else "Downloading: "
        suffix = f"{len(self.queue)} queued" if self.queue else ""
        self.dl_bar.cancel.setVisible(True)
        self.dl_bar.show_progress(prefix + d["item"].title, d["pct"], d["speed"], d["eta"], suffix)

    def _on_dl_progress(self, job, pct, speed, eta):
        d = self.downloads.get(job)
        if not d:
            return
        d.update(pct=pct, speed=speed, eta=eta)
        self._refresh_dl_bar()

    def _finish_job(self, job, state: str):
        d = self.downloads.pop(job, None)
        if not d:
            return None
        self.batch_done += 1
        self._set_card_state(d["item"].vid, state)
        if self.queue:
            self._pump()
        else:
            self.batch_done = 0
            self._idle_timer.start()
        return d

    def _on_dl_done(self, job, path):
        d = self._finish_job(job, "done")
        if d:
            self.status_label.setText(f"Saved: {path or d['item'].title}")
            if not self.downloads:
                self.dl_bar.show_progress("✓ " + d["item"].title, 100.0, "Done", "")
                self.dl_bar.cancel.setVisible(False)

    def _on_dl_failed(self, job, msg):
        cancelled = msg == "Cancelled"
        d = self._finish_job(job, "idle" if cancelled else "failed")
        if d:
            nxt = " — continuing with the next one" if self.downloads else ""
            self.status_label.setText(
                (f"Cancelled: {d['item'].title}" if cancelled
                 else f"Download failed — {d['item'].title}: {msg}") + nxt)
            if not self.downloads:
                self.dl_bar.setVisible(False)

    def cancel_downloads(self, choice: str | None = None):
        """Cancel button. With videos queued, ask whether to skip to the next
        one or cancel the whole queue. `choice` ("skip" / "all") bypasses the
        dialog (used by --self-test)."""
        if not self.downloads:
            return
        if self.queue and choice is None:
            box = QMessageBox(self)
            box.setWindowTitle("Cancel download")
            box.setText(f"Cancel the current download?\n\n{len(self.queue)} more video(s) are queued.")
            skip = box.addButton("Skip to next", QMessageBox.ButtonRole.AcceptRole)
            cancel_all = box.addButton("Cancel whole queue", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Keep downloading", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            choice = "skip" if clicked is skip else "all" if clicked is cancel_all else None
            if choice is None:
                return
        if choice == "all" or not self.queue:
            self._clear_queue()
        for d in self.downloads.values():
            d["worker"].cancel()
        self.status_label.setText("Cancelling…")

    def _clear_queue(self):
        while self.queue:
            self._set_card_state(self.queue.popleft()["item"].vid, "idle")

    # -- lifecycle -----------------------------------------------------------
    def keyPressEvent(self, ev):
        if self.cinema.isVisible() and ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q) \
                and not isinstance(QApplication.focusWidget(), QLineEdit):
            self.cinema.close_player()
            return
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        self.cfg.set("window_geometry", bytes(self.saveGeometry().toBase64()).decode(), save=False)
        self.cfg.data.update(last_channel=self.channel_edit.text().strip(),
                             last_query=self.search_edit.text().strip())
        self.cfg.save()
        self._clear_queue()
        for d in list(self.downloads.values()):
            d["worker"].cancel()
        for d in list(self.downloads.values()):
            d["worker"].wait(3000)
        self.cinema.shutdown()
        self.thumbs.stop()
        super().closeEvent(ev)


def _elide_lines(text: str, fm, width: int, lines: int) -> str:
    """Word-wrap text to at most `lines` lines, eliding the last one."""
    words, out, cur = text.split(), [], ""
    for i, word in enumerate(words):
        trial = f"{cur} {word}".strip()
        if fm.horizontalAdvance(trial) <= width or not cur:
            cur = trial
            continue
        out.append(cur)
        cur = word
        if len(out) == lines - 1:
            cur = " ".join(words[i:])
            break
    out.append(fm.elidedText(cur, Qt.TextElideMode.ElideRight, width))
    out = [fm.elidedText(l, Qt.TextElideMode.ElideRight, width) for l in out[:lines]]
    return "\n".join(out)


def _ago(ts: int) -> str:
    if not ts:
        return ""
    s = max(0, int(time.time()) - ts)
    for div, unit in ((31536000, "year"), (2592000, "month"), (604800, "week"),
                      (86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= div:
            n = s // div
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def _self_test(win: "MainWindow", report_path: str):
    """`--self-test <report.json>`: search -> embedded playback -> scrub bar
    (hold, release-seek, click-seek) -> Q / Esc close -> download, then write a
    JSON report and quit. Used to verify packaged builds, which have no
    console. Uses real input events (QtTest). Does not touch config.json."""
    import json
    import tempfile

    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest

    from player_widget import ca_bundle_path

    win.cfg.save = lambda: None
    dl_dir = tempfile.mkdtemp(prefix="musavk-selftest-")
    win.path_edit.setText(dl_dir)
    win.quality_box.setCurrentIndex(len(QUALITY_OPTIONS) - 1)   # smallest
    rep = {"frozen": bool(getattr(sys, "frozen", False)), "ca_bundle": ca_bundle_path(),
           "download_dir": dl_dir, "steps": {}}
    st = {"phase": "search", "t0": time.monotonic(), "phase_t": time.monotonic()}
    cin = win.cinema
    LMB = Qt.MouseButton.LeftButton

    def goto(phase, **kw):
        st.update(phase=phase, phase_t=time.monotonic(), **kw)

    def step(name, **data):
        rep["steps"][name] = dict(data, at_s=round(time.monotonic() - st["t0"], 1))

    def finish(ok: bool):
        rep["ok"] = ok and all(v.get("ok", True) for v in rep["steps"].values())
        rep["elapsed_s"] = round(time.monotonic() - st["t0"], 1)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, ensure_ascii=False)
        timer.stop()
        win.close()
        QApplication.quit()

    def slider_point(frac: float) -> QPoint:
        s = cin.slider
        return QPoint(int(7 + frac * (s.width() - 14)), s.height() // 2)

    def playing(min_pos=1.0):
        pos = cin.player.position()
        return pos is not None and pos > min_pos and (cin.player.duration() or 0) > 0

    def tick():
        ph, waited = st["phase"], time.monotonic() - st["phase_t"]
        p = cin.player.player
        if ph == "search":
            if win.cards and not win.loading:
                step("search", ok=True, results=len(win.cards))
                # A video long enough to scrub meaningfully.
                item = max((c.item for c in win.cards.values() if c.item.playable),
                           key=lambda i: min(i.duration, 1800))
                win.play(item)
                goto("play", item=item)
            elif waited > 30:
                step("search", ok=False, status=win.status_label.text())
                finish(False)
        elif ph == "play":
            if playing(1.5):
                step("play", ok=True, url=st["item"].url, duration_s=round(cin.player.duration(), 1),
                     position_s=round(cin.player.position(), 2), video=f"{p.width}x{p.height}",
                     status=cin.status.text(), tls_ca_file=p["tls-ca-file"],
                     slider_follows=cin.slider.value() > 0, time_label=cin.time_label.text())
                # Press and HOLD at 50 %: the poller must not move the handle.
                QTest.mousePress(cin.slider, LMB, Qt.KeyboardModifier.NoModifier, slider_point(0.5))
                goto("hold", held_value=cin.slider.value(), label_while_held=cin.time_label.text())
            elif waited > 45:
                step("play", ok=False, status=cin.status.text(), mpv_error=cin.player.load_error())
                finish(False)
        elif ph == "hold" and waited > 1.2:
            unchanged = cin.slider.value() == st["held_value"]
            target = st["held_value"] / 1000
            QTest.mouseRelease(cin.slider, LMB, Qt.KeyboardModifier.NoModifier, slider_point(0.5))
            step("scrub_hold", ok=unchanged and cin._dragging is False, held_ms=st["held_value"],
                 value_after_1_2s=cin.slider.value(), label_while_held=st["label_while_held"])
            goto("seek50", target=target)
        elif ph == "seek50":
            pos = cin.player.position()
            if pos is not None and abs(pos - st["target"]) < 3 and waited > 0.6:
                step("seek_release", ok=True, target_s=round(st["target"], 1), position_s=round(pos, 1),
                     time_label=cin.time_label.text())
                QTest.mouseClick(cin.slider, LMB, Qt.KeyboardModifier.NoModifier, slider_point(0.2))
                goto("seek20", target=cin.slider.value() / 1000)
            elif waited > 10:
                step("seek_release", ok=False, target_s=st["target"], position_s=pos)
                finish(False)
        elif ph == "seek20":
            pos = cin.player.position()
            if pos is not None and abs(pos - st["target"]) < 3 and waited > 0.6:
                step("seek_click", ok=True, target_s=round(st["target"], 1), position_s=round(pos, 1))
                # Shortcut focus: the slider was just used; Q must still close.
                fw = QApplication.focusWidget()
                QTest.keyClick(fw or cin, Qt.Key.Key_Q)
                goto("close_q", focus=type(fw).__name__ if fw else None)
            elif waited > 10:
                step("seek_click", ok=False, target_s=st["target"], position_s=pos)
                finish(False)
        elif ph == "close_q" and waited > 0.5:
            step("close_with_Q", ok=not cin.isVisible(), focus_widget=st["focus"],
                 mpv_idle=bool(p.idle_active))
            win.play(st["item"])
            goto("replay")
        elif ph == "replay":
            if playing(0.5):
                QTest.mouseClick(cin.slider, LMB, Qt.KeyboardModifier.NoModifier, slider_point(0.3))
                fw = QApplication.focusWidget()
                QTest.keyClick(fw or cin, Qt.Key.Key_Escape)
                goto("close_esc", focus=type(fw).__name__ if fw else None)
            elif waited > 45:
                step("close_with_Esc", ok=False, status=cin.status.text())
                finish(False)
        elif ph == "close_esc" and waited > 0.5:
            step("close_with_Esc", ok=not cin.isVisible(), focus_widget=st["focus"])
            # Throughput check: a ~3 min video at Best quality.
            win.quality_box.setCurrentIndex(0)
            item = min((c.item for c in win.cards.values() if c.item.playable),
                       key=lambda i: abs((i.duration or 0) - 180))
            win.download(item)
            goto("download", progress=0.0, speeds=[], dl_t0=time.monotonic())
            job = next(iter(win.downloads.values()))["worker"]

            def on_progress(_j, pct, speed, _eta):
                st["progress"] = max(st["progress"], pct)
                if "Mbit/s" in speed:
                    st["speeds"].append(float(speed.split("(")[1].split()[0]))
            job.progress.connect(on_progress)
        elif ph == "download":
            if not win.downloads:
                files = [f for f in os.listdir(dl_dir) if not f.endswith((".part", ".ytdl"))]
                ok = bool(files) and "Saved" in win.status_label.text()
                size = sum(os.path.getsize(os.path.join(dl_dir, f)) for f in files)
                el = time.monotonic() - st["dl_t0"]
                sp = st["speeds"]
                step("download", ok=ok, files=files, max_progress=round(st["progress"], 1),
                     size_mib=round(size / 2**20, 1), seconds_incl_extract=round(el, 1),
                     avg_mbit_s=round(size * 8 / el / 1e6, 1),
                     peak_mbit_s=max(sp) if sp else None,
                     median_mbit_s=sorted(sp)[len(sp) // 2] if sp else None,
                     status=win.status_label.text())
                if not ok:
                    finish(False)
                    return
                # Channel filter by display name, empty keyword -> channel feed.
                win.search_edit.setText("")
                win.channel_edit.setText("гуфи")
                win.start_search()
                goto("channel_feed")
            elif waited > 120:
                step("download", ok=False, status=win.status_label.text())
                finish(False)
        elif ph in ("channel_feed", "channel_search"):
            if win.cards and not win.loading:
                ci = win.channel_info or {}
                ch = ci.get("channel")
                owners = {int(v.rsplit("_", 1)[0]) for v in win.cards}
                step(ph, ok=bool(ch) and owners == {ch.owner_id}, channel=ch.label if ch else None,
                     results=len(win.cards), all_from_channel=bool(ch) and owners == {ch.owner_id},
                     status=win.status_label.text())
                if ph == "channel_feed":
                    # In-channel keyword search (handle + popular keyword).
                    win.search_edit.setText("гуфи")
                    win.channel_edit.setText("@gufee.medalin.kino")
                    win.start_search()
                    goto("channel_search")
                else:
                    # Batch queue: global search for short clips.
                    win.channel_edit.setText("")
                    win.search_edit.setText("смешные котики")
                    win.start_search()
                    goto("batch_search")
            elif waited > 40:
                step(ph, ok=False, status=win.status_label.text(), empty=win.empty_label.text())
                finish(False)
        elif ph == "batch_search":
            if win.cards and not win.loading:
                cards = sorted((c for c in win.cards.values() if c.item.playable and c.item.duration),
                               key=lambda c: c.item.duration)[:4]
                for c in cards:   # real clicks on the thumbnail checkboxes
                    QTest.mouseClick(c.check, LMB, Qt.KeyboardModifier.NoModifier, c.check.rect().center())
                vids = [c.item.vid for c in cards]
                step("batch_select", ok=(list(win.selected) == vids and not cin.isVisible()
                                         and win.batch_bar.isVisible()),
                     selected=len(win.selected), playback_opened=cin.isVisible(),
                     bar=win.batch_bar.label.text(), button=win.batch_bar.download.text(),
                     durations=[c.item.duration for c in cards])
                n_before = len(win.cards)
                win.load_more()
                goto("batch_more", vids=vids, n_before=n_before)
            elif waited > 30:
                step("batch_select", ok=False, status=win.status_label.text())
                finish(False)
        elif ph == "batch_more" and not win.loading and waited > 0.3:
            still = all(win.cards[v].check.isChecked() for v in st["vids"])
            new_unchecked = all(not c.check.isChecked() for v, c in win.cards.items() if v not in st["vids"])
            step("batch_persist_load_more", ok=still and new_unchecked and len(win.selected) == 4,
                 cards_before=st["n_before"], cards_after=len(win.cards))
            win.start_search()      # same query again: every card widget is rebuilt
            goto("batch_rebuild")
        elif ph == "batch_rebuild" and win.cards and not win.loading:
            present = [v for v in st["vids"] if v in win.cards]
            ok = all(win.cards[v].check.isChecked() for v in present) and len(win.selected) == 4
            step("batch_persist_new_search", ok=ok, selected_cards_visible=len(present),
                 bar=win.batch_bar.label.text())
            win.quality_box.setCurrentIndex(len(QUALITY_OPTIONS) - 1)
            batch_dir = os.path.join(dl_dir, "batch")
            win.path_edit.setText(batch_dir)
            QTest.mouseClick(win.batch_bar.download, LMB)
            goto("batch_run", batch_dir=batch_dir, max_active=0, order=[], labels=[],
                 skipped=None, last_tick=time.monotonic(), max_gap=0.0,
                 queued_after_click=len(win.queue), bar_hidden=not win.batch_bar.isVisible())
        elif ph == "batch_run":
            now = time.monotonic()
            st["max_gap"] = max(st["max_gap"], now - st["last_tick"])
            st["last_tick"] = now
            st["max_active"] = max(st["max_active"], len(win.downloads))
            active = next(iter(win.downloads.values()), None)
            if active and (not st["order"] or st["order"][-1] != active["item"].vid):
                st["order"].append(active["item"].vid)
            txt = f"{win.dl_bar.label.text()} · {win.dl_bar.speed.text()}"
            if active and txt not in st["labels"] and active["pct"] > 0:
                if not st["labels"] or st["labels"][-1].split(" · ")[0] != txt.split(" · ")[0]:
                    st["labels"].append(txt)
            # Cancel the first one mid-transfer -> "Skip to next".
            if st["skipped"] is None and active and active["pct"] > 0:
                st["skipped"] = active["item"].vid
                win.cancel_downloads("skip")
            if not win.downloads and not win.queue and st["skipped"]:
                files = sorted(f for f in os.listdir(st["batch_dir"]) if f.endswith(".mp4"))
                ids = [f[f.rindex("[") + 1:f.rindex("]")] for f in files]
                expect = [v for v in st["vids"] if v != st["skipped"]]
                ok = (st["max_active"] == 1 and st["order"] == st["vids"]
                      and sorted(ids) == sorted(expect) and st["queued_after_click"] == 3
                      and st["max_gap"] < 1.0)
                step("batch_queue", ok=ok, max_concurrent_downloads=st["max_active"],
                     fifo_order_kept=st["order"] == st["vids"], queued_after_click=st["queued_after_click"],
                     batch_bar_hidden_after_click=st["bar_hidden"], skipped_first=st["skipped"],
                     files=files, all_others_downloaded=sorted(ids) == sorted(expect),
                     longest_ui_stall_ms=round(st["max_gap"] * 1000), status_samples=st["labels"][:4],
                     card_states=[win.cards[v].dl_btn.text() for v in st["vids"] if v in win.cards])
                finish(ok)
            elif waited > 240:
                step("batch_queue", ok=False, status=win.status_label.text(), order=st["order"])
                finish(False)

    timer = QTimer(win)
    timer.timeout.connect(tick)
    timer.start(200)
    win.search_edit.setText("cats")
    win.channel_edit.setText("")
    win.start_search()


def main():
    # --windowed (no console) builds have sys.stdout/stderr = None; libraries
    # that print (yt-dlp error output) would crash writing to them.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("musavk.vkvideo")
        except Exception:
            pass
    # Warm up yt-dlp (slow first import) so the first play/resolve is snappy.
    import threading
    threading.Thread(target=lambda: __import__("yt_dlp"), daemon=True).start()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    import locale
    locale.setlocale(locale.LC_NUMERIC, "C")   # libmpv requires the C numeric locale
    win = MainWindow()
    win.show()
    if "--self-test" in sys.argv:
        i = sys.argv.index("--self-test")
        report = sys.argv[i + 1] if i + 1 < len(sys.argv) else "selftest-report.json"
        _self_test(win, os.path.abspath(report))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

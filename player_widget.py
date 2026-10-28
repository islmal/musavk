"""Embedded libmpv player and the cinema-mode overlay that hosts it."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import certifi
from PyQt6.QtCore import QEvent, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSlider, QStyle, QVBoxLayout,
                             QWidget)

from config import app_dir
from vk_search import UA, ResolvedStream, StreamResolver

_mpv = None
_mpv_error = ""


def _load_mpv():
    """Import python-mpv, first making likely libmpv locations visible.

    libmpv (mpv-2.dll / libmpv-2.dll on Windows) is not part of pip packages;
    put it next to main.py / the .exe, in ./lib, or anywhere on PATH. Frozen
    builds also carry a copy in the bundle dir (sys._MEIPASS); a DLL placed
    next to the .exe takes precedence, so libmpv can be updated in place.
    """
    global _mpv, _mpv_error
    if _mpv is not None or _mpv_error:
        return _mpv
    candidates = [app_dir(), app_dir() / "lib"]       # highest priority first
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates += [Path(sys._MEIPASS), Path(sys._MEIPASS) / "lib"]
    mpv_exe = shutil.which("mpv")
    if mpv_exe:
        candidates.append(Path(mpv_exe).resolve().parent)
    dirs = [str(d) for d in candidates if d.is_dir()]
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    if sys.platform == "win32":
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
    try:
        import mpv  # noqa: WPS433

        _mpv = mpv
    except (OSError, ImportError) as e:
        _mpv_error = str(e)
    return _mpv


def ca_bundle_path() -> str:
    """CA bundle for libmpv's TLS. In a PyInstaller build certifi's
    cacert.pem is bundled under <bundle>/certifi/."""
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", app_dir())) / "certifi" / "cacert.pem"
        if bundled.is_file():
            return str(bundled)
    return certifi.where()


class VideoSurface(QWidget):
    """Native child window that libmpv renders into via --wid."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setStyleSheet("background: black;")
        self.setMouseTracking(True)

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), Qt.GlobalColor.black)


class MpvPlayer(QWidget):
    closeRequested = pyqtSignal()
    statusChanged = pyqtSignal(str)
    _mpvKey = pyqtSignal(str)   # marshals mpv-thread key presses to the GUI thread
    _fileLoaded = pyqtSignal()
    _started = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.surface = VideoSurface(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.surface)
        self.player = None
        self._token = 0
        self._resolver: StreamResolver | None = None
        self._pending_audio = ""
        self._format_desc = ""
        self._mpvKey.connect(self._on_mpv_key)
        self._fileLoaded.connect(self._on_file_loaded)
        self._started.connect(lambda: self.statusChanged.emit(self._format_desc))

    @property
    def available(self) -> bool:
        return _load_mpv() is not None

    @staticmethod
    def load_error() -> str:
        _load_mpv()
        return _mpv_error

    def _ensure_player(self):
        if self.player is not None:
            return self.player
        mpv = _load_mpv()
        if mpv is None:
            raise RuntimeError(_mpv_error or "libmpv not available")
        self.player = mpv.MPV(
            log_handler=self._log, loglevel="error",
            wid=str(int(self.surface.winId())),
            ytdl=False,                 # streams are resolved by yt-dlp in Python
            hwdec="auto-safe",
            vo="gpu",
            osc=False,                  # the Qt scrub bar replaces mpv's OSC
            input_default_bindings=True,
            input_vo_keyboard=True,
            keep_open="no",
            cache=True,
            demuxer_max_bytes="256MiB",
            demuxer_readahead_secs=30,
            referrer="https://vkvideo.ru/",
            # libmpv's bundled curl ships without a usable CA store on Windows,
            # so VK CDN (okcdn.ru) TLS fails ("error -13"); use certifi's bundle.
            tls_verify=True,
            tls_ca_file=ca_bundle_path(),
        )

        # When the mpv window has keyboard focus Qt never sees the keys, so
        # bind the close keys inside mpv too (overrides default "q" = quit).
        for key in ("q", "Q", "ESC"):
            self.player.register_key_binding(key, lambda state, *_, k=key: (
                state.startswith("d") and self._mpvKey.emit(k)))

        @self.player.event_callback("end-file")
        def _end(ev):
            data = getattr(ev, "data", None)
            reason = getattr(data, "reason", None)
            err = getattr(data, "error", 0)
            if reason is not None and int(reason) == 4:  # MPV_END_FILE_REASON_ERROR
                self.statusChanged.emit(f"Playback failed (error {err}). Video may be restricted.")

        @self.player.event_callback("file-loaded")
        def _loaded(_ev):
            self._fileLoaded.emit()

        @self.player.event_callback("playback-restart")
        def _restart(_ev):
            self._started.emit()

        return self.player

    def _log(self, level, component, message):
        if level in ("error", "fatal") and component in ("cplayer", "stream", "ffmpeg", "demux"):
            self.statusChanged.emit(message.strip())

    def _on_mpv_key(self, _key: str):
        self.closeRequested.emit()

    def play(self, page_url: str, fmt: str):
        """Resolve the VK page with yt-dlp in a worker thread, then play the
        direct stream URL (mpv's own ytdl hook is not used)."""
        self._ensure_player()
        self.stop()
        self._token += 1
        self.statusChanged.emit("Resolving stream…")
        r = StreamResolver(self._token, page_url, fmt, self)
        r.resolved.connect(self._on_resolved)
        r.failed.connect(self._on_resolve_failed)
        r.finished.connect(r.deleteLater)
        self._resolver = r
        r.start()

    def _on_resolved(self, token: int, stream: ResolvedStream):
        if token != self._token or self.player is None:
            return   # closed or superseded meanwhile
        h = stream.headers
        self.player["user-agent"] = h.get("User-Agent") or UA
        self.player["referrer"] = h.get("Referer") or "https://vkvideo.ru/"
        extra = [f"{k}: {v}" for k, v in h.items()
                 if k.lower() in ("cookie", "origin", "accept-language") and v]
        self.player["http-header-fields"] = extra
        self._pending_audio = stream.audio_url
        self._format_desc = stream.format_desc
        self.statusChanged.emit("Buffering…")
        self.player.pause = False
        self.player.play(stream.url)

    def _on_file_loaded(self):
        # Separate audio track (video-only format chosen): attach it once loaded.
        if self._pending_audio and self.player is not None:
            try:
                self.player.command("audio-add", self._pending_audio, "select")
            except Exception as e:
                self.statusChanged.emit(f"Audio track failed: {e}")
            self._pending_audio = ""

    def _on_resolve_failed(self, token: int, msg: str):
        if token == self._token:
            self.statusChanged.emit(f"Could not resolve stream: {msg}")

    def stop(self):
        self._token += 1          # invalidate any in-flight resolve
        self._pending_audio = ""
        if self.player is not None:
            try:
                self.player.command("stop")
            except Exception:
                pass

    def toggle_pause(self):
        if self.player is not None:
            self.player.pause = not self.player.pause

    def seek(self, seconds: float):
        if self.player is not None:
            try:
                self.player.seek(seconds, reference="relative")
            except Exception:
                pass

    def seek_absolute(self, seconds: float):
        if self.player is not None:
            try:
                self.player.seek(max(0.0, seconds), reference="absolute")
            except Exception:
                pass

    def _prop(self, name: str):
        if self.player is None:
            return None
        try:
            return getattr(self.player, name)
        except Exception:
            return None

    def position(self) -> float | None:
        return self._prop("time_pos")

    def duration(self) -> float | None:
        return self._prop("duration")

    def is_paused(self) -> bool:
        return bool(self._prop("pause"))

    def shutdown(self):
        if self.player is not None:
            try:
                self.player.terminate()
            except Exception:
                pass
            self.player = None


def fmt_time(seconds: float | None) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ScrubSlider(QSlider):
    """Horizontal timeline in milliseconds. Clicking the groove jumps the
    handle under the cursor and starts a drag, so click-to-seek and drag share
    one path (sliderPressed ... sliderReleased). It never takes keyboard focus,
    so Q / Esc / Space / arrows keep working."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setRange(0, 0)
        self.setFixedHeight(22)
        self.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; background: #2c303c; border-radius: 2px; }"
            "QSlider::sub-page:horizontal { background: #3b6cff; border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #ffffff; width: 14px; height: 14px;"
            " margin: -5px 0; border-radius: 7px; }"
            "QSlider::handle:horizontal:hover { background: #cfdcff; }"
            "QSlider::sub-page:horizontal:disabled { background: #2c303c; }"
            "QSlider::handle:horizontal:disabled { background: #4a4f5c; }")

    def _value_at(self, x: float) -> int:
        handle = 14
        span = max(1, self.width() - handle)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                              int(x - handle / 2), span)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            self.setValue(self._value_at(ev.position().x()))
        super().mousePressEvent(ev)   # handle is now under the cursor -> drag

    def wheelEvent(self, ev):
        ev.ignore()   # no accidental seeks while scrolling


class CinemaOverlay(QWidget):
    """Semi-transparent (80% black) overlay covering its parent widget, with
    the player centered at ~80% of the available area."""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

        self.frame = QWidget(self)
        self.frame.setObjectName("cinemaFrame")
        self.frame.setStyleSheet("#cinemaFrame { background: #000; border-radius: 10px; }")
        v = QVBoxLayout(self.frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QWidget(self.frame)
        header.setObjectName("cinemaHeader")
        header.setStyleSheet("#cinemaHeader { background: #111318; border-top-left-radius: 10px;"
                             " border-top-right-radius: 10px; }")
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 6, 6, 6)
        self.title = QLabel("", header)
        self.title.setStyleSheet("color: #e8eaf0; font-size: 14px; font-weight: 600;")
        self.status = QLabel("", header)
        self.status.setStyleSheet("color: #8a91a5; font-size: 12px;")
        self.close_btn = QPushButton("✕", header)
        self.close_btn.setToolTip("Close (Q / Esc)")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFixedSize(40, 40)
        self.close_btn.setStyleSheet(
            "QPushButton { color: #fff; background: transparent; border: none; font-size: 22px;"
            " border-radius: 20px; } QPushButton:hover { background: #e5484d; }")
        self.close_btn.clicked.connect(self.close_player)
        h.addWidget(self.title, 1)
        h.addWidget(self.status)
        h.addSpacing(8)
        h.addWidget(self.close_btn)
        v.addWidget(header)

        self.player = MpvPlayer(self.frame)
        self.player.closeRequested.connect(self.close_player)
        self.player.statusChanged.connect(self.status.setText)
        v.addWidget(self.player, 1)

        self.controls = QWidget(self.frame)
        self.controls.setObjectName("cinemaControls")
        self.controls.setStyleSheet(
            "#cinemaControls { background: #111318; border-bottom-left-radius: 10px;"
            " border-bottom-right-radius: 10px; }")
        c = QHBoxLayout(self.controls)
        c.setContentsMargins(10, 4, 14, 4)
        c.setSpacing(10)
        self.play_btn = QPushButton("❚❚", self.controls)
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_btn.setFixedSize(34, 30)
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.setStyleSheet(
            "QPushButton { color: #fff; background: transparent; border: none; font-size: 14px; padding: 0;"
            " border-radius: 6px; } QPushButton:hover { background: #252a35; }")
        self.play_btn.clicked.connect(self.player.toggle_pause)
        self.slider = ScrubSlider(self.controls)
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderMoved.connect(self._on_scrub_move)
        self.slider.sliderReleased.connect(self._on_scrub_release)
        self.time_label = QLabel("00:00 / 00:00", self.controls)
        self.time_label.setStyleSheet(
            "color: #c9cedb; font-size: 12px; font-family: Consolas, 'Cascadia Mono', monospace;")
        c.addWidget(self.play_btn)
        c.addWidget(self.slider, 1)
        c.addWidget(self.time_label)
        v.addWidget(self.controls)

        self._dragging = False
        self._seek_target: float | None = None   # hold the handle here until mpv catches up
        self._seek_deadline = 0.0
        self._poll = QTimer(self, interval=200)
        self._poll.timeout.connect(self._sync_timeline)

        self.error_label = QLabel("", self.frame)
        self.error_label.setWordWrap(True)
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setStyleSheet("color: #ff9b9b; padding: 30px; font-size: 13px;")
        self.error_label.hide()
        v.addWidget(self.error_label, 1)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for seq, fn in (("Q", self.close_player), ("Esc", self.close_player),
                        ("Space", self.player.toggle_pause),
                        ("Left", lambda: self.player.seek(-5)), ("Right", lambda: self.player.seek(5))):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(ctx)
            sc.activated.connect(fn)

        parent.installEventFilter(self)

    # -- geometry ---------------------------------------------------------
    def eventFilter(self, obj, ev):
        if obj is self.parent() and ev.type() == QEvent.Type.Resize:
            self._relayout()
        return False

    def _relayout(self):
        self.setGeometry(self.parent().rect())
        r = self.rect()
        chrome_h = 52 + 38   # header + control bar
        max_w, max_h = int(r.width() * 0.8), int(r.height() * 0.8)
        w = max_w
        h = int(w * 9 / 16) + chrome_h
        if h > max_h:
            h = max_h
            w = int((h - chrome_h) * 16 / 9)
        self.frame.setGeometry(QRect((r.width() - w) // 2, (r.height() - h) // 2, w, h))

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 204))   # 80% opacity

    def mousePressEvent(self, ev):
        # Click on the dimmed backdrop closes the player.
        if not self.frame.geometry().contains(ev.position().toPoint()):
            self.close_player()

    # -- timeline -----------------------------------------------------------
    def _reset_timeline(self):
        self._dragging = False
        self._seek_target = None
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.time_label.setText("00:00 / 00:00")
        self.play_btn.setText("❚❚")

    def _sync_timeline(self):
        dur = self.player.duration()
        pos = self.player.position()
        self.play_btn.setText("▶" if self.player.is_paused() else "❚❚")
        if not dur or dur <= 0:              # not loaded yet / live stream
            self.slider.setEnabled(False)
            if pos is not None:
                self.time_label.setText(f"{fmt_time(pos)} / --:--")
            return
        dur_ms = int(dur * 1000)
        if self.slider.maximum() != dur_ms:
            self.slider.setRange(0, dur_ms)
        self.slider.setEnabled(True)
        if self._dragging:
            return                           # user owns the handle: no jitter
        if self._seek_target is not None:
            # Until mpv reports the new position, keep showing the target
            # instead of snapping back to the pre-seek time.
            if (pos is not None and abs(pos - self._seek_target) < 1.5) \
                    or time.monotonic() > self._seek_deadline:
                self._seek_target = None
            else:
                pos = self._seek_target
        if pos is None:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(int(pos * 1000))
        self.slider.blockSignals(False)
        self.time_label.setText(f"{fmt_time(pos)} / {fmt_time(dur)}")

    def _on_scrub_start(self):
        self._dragging = True
        self._on_scrub_move(self.slider.value())

    def _on_scrub_move(self, value_ms: int):
        self.time_label.setText(f"{fmt_time(value_ms / 1000)} / {fmt_time(self.player.duration())}")

    def _on_scrub_release(self):
        target = self.slider.value() / 1000
        self._dragging = False
        self._seek_target = target
        self._seek_deadline = time.monotonic() + 2.0
        self.player.seek_absolute(target)
        self.setFocus()   # keep keyboard shortcuts on the overlay

    # -- API --------------------------------------------------------------
    def open(self, url: str, title: str, fmt: str = "best"):
        self._relayout()
        self.title.setText(title)
        self.status.setText("")
        self._reset_timeline()
        self.show()
        self.raise_()
        self.setFocus()
        if not self.player.available:
            self.player.hide()
            self.controls.hide()
            self.error_label.setText(
                "libmpv could not be loaded, so embedded playback is unavailable.\n\n"
                "Download a libmpv build for Windows (e.g. the “mpv-dev-x86_64” archive from "
                "github.com/shinchiro/mpv-winbuild-cmake/releases) and put libmpv-2.dll next to "
                "main.py or in a ./lib folder, then restart.\n\n" + MpvPlayer.load_error())
            self.error_label.show()
            return
        self.error_label.hide()
        self.player.show()
        self.controls.show()
        self._poll.start()
        try:
            self.player.play(url, fmt)
        except Exception as e:
            self.status.setText(f"Player error: {e}")

    def close_player(self):
        if not self.isVisible():
            return
        self._poll.stop()
        self.player.stop()
        self._reset_timeline()
        self.hide()
        self.closed.emit()

    def shutdown(self):
        self.player.shutdown()

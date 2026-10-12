"""Letterboxed video view that other widgets can be layered on top of.

QVideoWidget renders into a native child window that covers sibling widgets,
so the frame is drawn as a QGraphicsVideoItem in a QGraphicsView instead.

Two display modes:
  fit    - whole frame letterboxed into the view, smooth (bilinear) scaling
  focus  - 1:1 camera pixels (zoom 1x..8x with the wheel, drag to pan), no
           smoothing, so single-pixel focus-assist dots stay exact

OpenGL viewport is off by default: on this Wayland + NVIDIA setup it renders
white, and under XWayland (QT_QPA_PLATFORM=xcb) it crashes. Raster costs
~50% of one core per 1080p60 feed.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QPainter, QTransform
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QFrame, QGraphicsScene, QGraphicsView, QLabel

from .capture import Capture

STATE_TEXT = {
    "starting": "Starting…",
    "no-signal": "NO SIGNAL\ncheck camera power and HDMI output",
    "missing": "CAPTURE DEVICE MISSING\nwaiting for the Cam Link to come back",
    "error": "CAPTURE ERROR\nretrying",
    "stopped": "Stopped",
}
MAX_ZOOM = 8


class VideoView(QGraphicsView):
    zoomChanged = Signal(object)  # None in fit mode, else the zoom factor
    doubleClicked = Signal()

    def __init__(self, label: str = "", opengl: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.label = label
        self._capture: Capture | None = None
        self.zoom: int | None = None  # None = fit
        if opengl:
            self.setViewport(QOpenGLWidget())
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(Qt.GlobalColor.black))
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.video = QGraphicsVideoItem()
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)  # letterbox
        self._scene.addItem(self.video)

        self._message = QLabel(self)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setStyleSheet("color: #e6e6e6; font: bold 16pt 'Sans'; background: transparent")
        self._debug = QLabel(self)
        self._debug.setStyleSheet("color: #ffdc00; font: 11pt 'Monospace'; "
                                  "background: rgba(0,0,0,170); padding: 4px 6px")
        self._debug.hide()
        self._zoom_badge = QLabel(self)
        self._zoom_badge.setStyleSheet("color: white; font: bold 11pt 'Sans'; "
                                       "background: rgba(0,90,170,200); padding: 3px 8px")
        self._zoom_badge.hide()
        for w in (self._message, self._debug, self._zoom_badge):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._debug_timer = QTimer(self, interval=500, timeout=self._update_debug)
        self._apply_mode()

    # --- capture ------------------------------------------------------------------------

    def attach(self, capture: Capture) -> None:
        if self._capture is not None:
            self._capture.frame.disconnect(self._show_frame)
            self._capture.stateChanged.disconnect(self._on_state)
        self._capture = capture
        capture.frame.connect(self._show_frame)
        capture.stateChanged.connect(self._on_state)
        self._on_state(capture.state, "")

    def show_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))
        self._place_overlays()

    def _show_frame(self, frame) -> None:
        self.video.videoSink().setVideoFrame(frame)

    def _on_state(self, state: str, detail: str) -> None:
        text = STATE_TEXT.get(state, "")
        if text and self.label:
            text = f"{self.label}\n{text}"
        self.show_message(text)
        if state != "streaming":
            self.video.videoSink().setVideoFrame(QVideoFrame())

    # --- debug overlay ------------------------------------------------------------------

    def set_debug(self, on: bool) -> None:
        self._debug.setVisible(on)
        if on:
            self._debug_timer.start()
            self._update_debug()
        else:
            self._debug_timer.stop()

    def debug_visible(self) -> bool:
        return self._debug.isVisible()

    def _update_debug(self) -> None:
        c = self._capture
        if c is None:
            return
        s = c.stats()
        lat = f"{s.latency_ms:5.1f} ms" if s.latency_ms is not None else "n/a"
        view = "fit (smooth)" if self.zoom is None else f"1:1 x{self.zoom} (pixel exact)"
        self._debug.setText(
            f"{self.label} [{c.backend}] {c.state}\n"
            f"capture {s.width}x{s.height} {s.pixel_format}  {s.fps:4.1f} fps "
            f"(asked {c.width}x{c.height}@{c.fps})\n"
            f"frames {s.frames}  capture->app {lat}\n"
            f"view {view}\n"
            f"{c.by_id.rsplit('/', 1)[-1]}"
        )
        self._debug.adjustSize()

    # --- fit / 1:1 focus view -----------------------------------------------------------

    def set_zoom(self, zoom: int | None) -> None:
        """None = fit to view; 1..MAX_ZOOM = camera pixels at that magnification."""
        if zoom is not None:
            zoom = max(1, min(MAX_ZOOM, zoom))
        if zoom == self.zoom:
            return
        entering = self.zoom is None and zoom is not None
        self.zoom = zoom
        self._apply_mode(recenter=entering)
        self.zoomChanged.emit(zoom)

    def toggle_focus_view(self) -> None:
        self.set_zoom(None if self.zoom is not None else 1)

    def _native_size(self) -> QSizeF:
        n = self.video.nativeSize()
        if n.isEmpty() and self._capture is not None:
            n = QSizeF(self._capture.width, self._capture.height)
        return n

    def _apply_mode(self, recenter: bool = False) -> None:
        if self.zoom is None:
            self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setTransform(QTransform())
            self._zoom_badge.hide()
        else:
            # Pixel-exact: no filtering, so each camera pixel is a crisp block.
            self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setTransform(QTransform.fromScale(self.zoom, self.zoom))
            self._zoom_badge.setText(f"1:1 ×{self.zoom}  ·  wheel zoom · drag · Z exit")
            self._zoom_badge.adjustSize()
            self._zoom_badge.show()
        self._layout()
        if recenter:
            self.centerOn(self.video.boundingRect().center())

    def wheelEvent(self, event) -> None:
        if self.zoom is None:
            return super().wheelEvent(event)
        step = 1 if event.angleDelta().y() > 0 else -1
        self.set_zoom(self.zoom + step)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        self.doubleClicked.emit()
        event.accept()

    # --- geometry -----------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()

    def _layout(self) -> None:
        if self.zoom is None:
            r = QRectF(self.viewport().rect())
            self.video.setSize(r.size())
            self._scene.setSceneRect(r)
        else:
            n = self._native_size()
            self.video.setSize(n)
            self._scene.setSceneRect(QRectF(0, 0, n.width(), n.height()))
        self.video.setPos(0, 0)
        self._place_overlays()

    def _place_overlays(self) -> None:
        w, h = self.width(), self.height()
        self._message.setGeometry(0, 0, w, h)
        self._debug.move(8, 8)
        # bottom centre, clear of the tile's status bar and ⚙ button
        self._zoom_badge.move((w - self._zoom_badge.width()) // 2, h - self._zoom_badge.height() - 34)

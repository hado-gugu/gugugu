"""A brief, input-transparent flight for the existing app icon."""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from branding import APP_NAME
import native


class WingFlight(QWidget):
    """Call start() to fly, and delete the widget after finished is emitted."""

    finished = Signal()
    _SIDE = 76

    def __init__(self, pixmap, screen, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus |
                         Qt.WindowType.WindowTransparentForInput | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowTitle(f"{APP_NAME} · 萨科塔之翼")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setStyleSheet("background: transparent; border: none;")
        self.setAutoFillBackground(False)
        self._sprite = pixmap.scaled(144, 144, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation) if not pixmap.isNull() else pixmap
        self._flight_screen = screen if screen is not None else QGuiApplication.primaryScreen()
        if self._flight_screen is not None:
            self._flight_screen.destroyed.connect(self._screen_lost)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)
        self._clock = QElapsedTimer()
        self._running = False
        self._facing = 1
        self._elapsed = 0.0
        self._duration = 0.0
        self._phase = 0.0

    def start(self) -> bool:
        if (self._running or self._sprite.isNull() or self._flight_screen is None
                or not self._flight_screen.availableGeometry().isValid()):
            return False
        self._duration = random.uniform(5.0, 10.0)
        self._phase = random.uniform(0.0, math.tau)
        self._facing = random.choice((-1, 1))
        xs = (random.uniform(.04, .18), random.uniform(.25, .44),
              random.uniform(.56, .75), random.uniform(.82, .96))
        self._xs = xs if self._facing == 1 else tuple(1.0 - x for x in xs)
        self._ys = tuple(random.uniform(.10, .90) for _ in xs)
        self.setScreen(self._flight_screen)
        self._running = True
        self._clock.start()
        self._advance()
        self.show()
        if QGuiApplication.platformName() == "windows":
            try:
                native.configure_overlay(int(self.winId()))
            except native.NativeError:
                pass  # The Qt input-transparency flags still apply.
        self._timer.start()
        return True

    def _advance(self):
        if not self._running:
            return
        self._elapsed = self._clock.elapsed() / 1000.0
        if self._elapsed >= self._duration or self._flight_screen is None:
            self._finish()
            return
        area = self._flight_screen.availableGeometry()
        if not area.isValid():
            self._finish()
            return
        side = min(self._SIDE, area.width(), area.height())
        if self.width() != side or self.height() != side:
            self.setFixedSize(side, side)
        t = self._elapsed / self._duration
        u = 1.0 - t
        weights = (u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3)
        x = sum(weight * position for weight, position in zip(weights, self._xs))
        y = sum(weight * position for weight, position in zip(weights, self._ys))
        horizontal = area.width() - side
        vertical = area.height() - side
        bob = 7.0 * math.sin(self._elapsed * math.tau * 1.4 + self._phase)
        self.move(area.left() + round(x * horizontal),
                  area.top() + round(max(0.0, min(vertical, y * vertical + bob))))
        self.update()

    def paintEvent(self, event):
        if self._sprite.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(self.width() / 2, self.height() / 2)
        phase = self._elapsed * math.tau * 1.4 + self._phase
        painter.rotate(8.0 * math.sin(phase) * self._facing)
        painter.scale(self._facing, .96 + .05 * math.sin(phase * 2))
        painter.setOpacity(max(0.0, min(1.0, (self._duration - self._elapsed) / .25)))
        scale = self.width() * .78 / max(self._sprite.width(), self._sprite.height())
        width, height = self._sprite.width() * scale, self._sprite.height() * scale
        painter.drawPixmap(QRectF(-width / 2, -height / 2, width, height), self._sprite,
                           QRectF(self._sprite.rect()))

    def _screen_lost(self):
        self._flight_screen = None
        self._finish()

    def _finish(self):
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        self.hide()
        self.finished.emit()

    def closeEvent(self, event):
        self._finish()
        super().closeEvent(event)

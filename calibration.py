"""Window-relative calibration and a screenshot-only selection surface."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import uuid


def validate_roi(roi):
    if not isinstance(roi, (list, tuple)) or len(roi) != 4:
        raise ValueError("请先拖动框选商店。")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in roi):
        raise ValueError("识别范围无效。")
    left, top, right, bottom = map(float, roi)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("识别范围必须在窗口画面内。")
    if right - left < .04 or bottom - top < .025:
        raise ValueError("框选范围太小，请包含完整商店卡片。")
    return [left, top, right, bottom]


def profile_key(identity):
    raw = str(identity.get("exe", "")).casefold() + "|" + str(identity.get("class_name", ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def profile_matches(profile, width, height, tolerance=.035):
    try:
        _validate_profile(profile)
        if type(width) not in (int, float) or type(height) not in (int, float):
            return False
        if not math.isfinite(width) or not math.isfinite(height) or min(width, height) <= 0:
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    reference = profile["reference_size"]
    return abs((width / height) / (reference[0] / reference[1]) - 1) <= tolerance


def _validate_reference_size(reference):
    if (not isinstance(reference, (list, tuple)) or len(reference) != 2
            or any(type(value) is not int or value <= 0 or value > 65536 for value in reference)):
        raise ValueError("校准画面尺寸无效。")
    return list(reference)


def _validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("校准记录无效。")
    validate_roi(profile.get("roi"))
    _validate_reference_size(profile.get("reference_size"))
    for key in ("layout", "title_hint", "exe"):
        if key in profile and not isinstance(profile[key], str):
            raise ValueError("校准记录无效。")
    return profile


def _decode_profiles(raw):
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int
                or payload["version"] != 1 or not isinstance(payload.get("profiles"), dict)):
            raise ValueError("校准文件格式无效。")
        for key, profile in payload["profiles"].items():
            if not isinstance(key, str) or not key:
                raise ValueError("校准记录标识无效。")
            _validate_profile(profile)
        return payload
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("校准文件无法读取；原文件保留，重新框选并保存范围即可恢复。") from error


def _backup_corrupt(path, raw):
    """Complete an exclusive backup before replacing any unreadable profile file."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.stem}.corrupt-{stamp}-{uuid.uuid4().hex}{path.suffix}")
    # O_EXCL prevents replacing a previous backup even in a name collision.
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # This unique path was created by us. Never touch the original here.
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    return backup


def _salvage_profiles(raw):
    """On explicit save, retain valid same-version entries beside damaged ones."""
    result = {"version": 1, "profiles": {}}
    try:
        original = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(original, dict) or type(original.get("version")) is not int
                or original["version"] != 1 or not isinstance(original.get("profiles"), dict)):
            return result
        for key, profile in original["profiles"].items():
            try:
                if not isinstance(key, str) or not key:
                    continue
                _validate_profile(profile)
            except (ValueError, TypeError, OverflowError):
                continue
            result["profiles"][key] = profile
    except (ValueError, TypeError, OverflowError):
        pass
    return result


def save_profile(path, identity, roi, reference_size):
    path = Path(path)
    roi = validate_roi(roi)
    width, height = _validate_reference_size(reference_size)
    payload = {"version": 1, "profiles": {}}
    if path.exists():
        raw = path.read_bytes()
        try:
            payload = _decode_profiles(raw)
        except ValueError:
            # Recovery is authorized by this explicit calibration save. Reading
            # a broken file never changes it, and a failed backup aborts saving.
            _backup_corrupt(path, raw)
            payload = _salvage_profiles(raw)
    profile = {"roi": roi, "reference_size": [width, height], "layout": "web-shop-v1",
               "title_hint": str(identity.get("title", "")), "exe": str(identity.get("exe", ""))}
    payload["profiles"][profile_key(identity)] = profile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="calibration-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return profile


def load_profiles(path):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "profiles": {}}
    return _decode_profiles(path.read_bytes())


def open_calibration(frame_bgr, existing_roi=None, parent=None, *, opacity=1.0):
    from PySide6.QtCore import Qt, QPointF, QRectF
    from PySide6.QtGui import QImage, QPainter, QColor, QPen
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QWidget, QDialogButtonBox
    import numpy as np

    pixels = np.ascontiguousarray(frame_bgr)
    height, width = pixels.shape[:2]
    image = QImage(pixels.data, width, height, int(pixels.strides[0]), QImage.Format.Format_BGR888).copy()

    class Selection(QWidget):
        def __init__(self):
            super().__init__()
            self.roi = existing_roi
            self.start = None
            self.dragging = False
            self.setMinimumSize(420, 260)
            self.setMouseTracking(True)

        def image_rect(self):
            ratio = min(self.width() / width, self.height() / height)
            return QRectF((self.width() - width * ratio) / 2, (self.height() - height * ratio) / 2,
                          width * ratio, height * ratio)

        def point(self, position):
            rect = self.image_rect()
            return QPointF(max(0., min(1., (position.x() - rect.left()) / rect.width())),
                           max(0., min(1., (position.y() - rect.top()) / rect.height())))

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton and self.image_rect().contains(event.position()):
                self.start = self.point(event.position())
                self.dragging = True

        def mouseMoveEvent(self, event):
            if self.dragging:
                end = self.point(event.position())
                self.roi = [min(self.start.x(), end.x()), min(self.start.y(), end.y()),
                            max(self.start.x(), end.x()), max(self.start.y(), end.y())]
                self.update()

        def mouseReleaseEvent(self, event):
            if self.dragging:
                self.mouseMoveEvent(event)
                self.dragging = False

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#080e0b"))
            rect = self.image_rect()
            painter.drawImage(rect, image)
            if self.roi:
                left, top, right, bottom = self.roi
                selected = QRectF(rect.x() + left * rect.width(), rect.y() + top * rect.height(),
                                  (right - left) * rect.width(), (bottom - top) * rect.height())
                painter.setPen(QPen(QColor("#59f4ca"), 3))
                painter.setBrush(QColor(89, 244, 202, 20))
                painter.drawRect(selected)

    dialog = QDialog(parent)
    from branding import APP_NAME
    dialog.setWindowTitle(f"{APP_NAME} · 框选商店")
    dialog.setWindowOpacity(opacity)
    dialog.resize(1100, 740)
    layout = QVBoxLayout(dialog)
    tip = QLabel("在截图上拖动框选整个商店卡片区域。保存后会跟随所选窗口；不需要逐个框名称。")
    tip.setWordWrap(True)
    layout.addWidget(tip)
    selection = Selection()
    layout.addWidget(selection, 1)
    hint = QLabel(f"建议保留卡片边缘，避开棋盘和{APP_NAME}窗口。当前预览来自绑定窗口的实际捕获画面。")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存范围")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    layout.addWidget(buttons)

    def accept():
        try:
            validate_roi(selection.roi)
        except ValueError as error:
            hint.setText(str(error))
            return
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return validate_roi(selection.roi)
    return None

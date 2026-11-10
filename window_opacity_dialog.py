"""Live opacity preview for the companion's windows."""

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QVBoxLayout)

from branding import APP_NAME
from opacity_settings import DEFAULT_OPACITY, MAX_OPACITY, MIN_OPACITY, WINDOW_LABELS


class WindowOpacityDialog(QDialog):
    def __init__(self, parent, preview, save):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self._preview = preview
        self._save = save
        self._original = {}
        self._values = {}
        self.setWindowTitle(f"{APP_NAME} · 窗口透明度")
        self.setMinimumWidth(310)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择要调整的窗口"))
        self.window_selector = QComboBox()
        for key, label in WINDOW_LABELS.items():
            self.window_selector.addItem(label, key)
        layout.addWidget(self.window_selector)

        slider_row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(MIN_OPACITY, MAX_OPACITY)
        self.slider.setAccessibleName("不透明度")
        self.percent = QLabel("100%")
        self.percent.setMinimumWidth(44)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider_row.addWidget(QLabel("不透明度"))
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.percent)
        layout.addLayout(slider_row)
        layout.addWidget(QLabel("数值越低越透明 · 调整时实时预览"))

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        reset = QPushButton("恢复当前项")
        reset.clicked.connect(lambda: self.slider.setValue(DEFAULT_OPACITY))
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save_button = QPushButton("保存")
        save_button.setObjectName("primary")
        save_button.clicked.connect(self.accept)
        actions.addWidget(reset)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save_button)
        layout.addLayout(actions)

        self.window_selector.currentIndexChanged.connect(self._select_window)
        self.slider.valueChanged.connect(self._change_opacity)

    def load_values(self, values):
        self._original = dict(values)
        self._values = dict(values)
        self.error.hide()
        self._select_window()

    def _select_window(self):
        value = self._values[self.window_selector.currentData()]
        with QSignalBlocker(self.slider):
            self.slider.setValue(value)
        self.percent.setText(f"{value}%")

    def _change_opacity(self, value):
        key = self.window_selector.currentData()
        self._values[key] = value
        self.percent.setText(f"{value}%")
        self._preview(key, value)

    def accept(self):
        changes = {key: value for key, value in self._values.items() if value != self._original[key]}
        try:
            if changes:
                self._save(changes)
        except (OSError, ValueError, TypeError) as error:
            self.error.setText(f"保存失败。{error}")
            self.error.show()
            return
        super().accept()

    def reject(self):
        for key, value in self._original.items():
            if value != self._values[key]:
                self._preview(key, value)
        super().reject()

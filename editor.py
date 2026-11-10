"""Separate native Qt preset window; closing it does not stop the companion."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QTabBar, QTextEdit,
    QVBoxLayout, QWidget,
)

from planner import Catalog, PlannerError, PlannerStore
from branding import APP_NAME, ICON_PATH
from operator_reference import load_operator_reference

PROFESSIONS = {
    "PIONEER": "先锋", "WARRIOR": "近卫", "TANK": "重装", "SNIPER": "狙击",
    "CASTER": "术师", "MEDIC": "医疗", "SUPPORT": "辅助", "SPECIAL": "特种",
}

STYLE = """
QMainWindow, QDialog { background:#0c1412; color:#e6f0eb; }
QWidget { color:#e6f0eb; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:12px; }
QLabel#muted { color:#92aaa1; }
QLabel#title { font-size:22px; font-weight:700; letter-spacing:2px; }
QLabel#section { font-size:14px; font-weight:700; color:#72e2bd; }
QLabel#status { color:#b7d4c8; padding:6px; background:#14221c; }
QFrame#panel { background:#111f19; border:1px solid #284337; border-radius:6px; }
QPushButton { padding:7px 10px; background:#1b3026; color:#d5e8df; border:1px solid #365245; border-radius:4px; }
QPushButton:hover { background:#28493a; border-color:#72e2bd; }
QPushButton:pressed { background:#37634e; }
QPushButton:disabled { color:#62756a; background:#142019; }
QPushButton#primary { background:#75dfb8; border-color:#75dfb8; color:#0d2419; font-weight:700; }
QPushButton#remove { padding:2px 5px; background:transparent; border:none; color:#9ebbad; }
QLineEdit, QComboBox, QTextEdit { background:#09130e; color:#e7f1eb; border:1px solid #355244; border-radius:4px; padding:6px; }
QComboBox QAbstractItemView { background:#14251c; color:#e7f1eb; selection-background-color:#315b44; }
QListWidget { background:#0d1912; border:1px solid #284337; border-radius:4px; outline:0; }
QListWidget::item { padding:6px; border:1px solid transparent; border-radius:4px; }
QListWidget::item:hover { background:#1a3425; border-color:#3e654f; }
QListWidget::item:selected { background:#244632; border-color:#68cfa3; }
QListWidget#roster::item { background:#14251a; border:1px solid #2a4333; }
QListWidget#roster::item:hover { background:#203c2a; border-color:#68cfa3; }
QListWidget::indicator { width:14px; height:14px; border:1px solid #719981; background:#0b1810; border-radius:3px; }
QListWidget::indicator:checked { background:#75dfb8; border:2px solid #b2ffdc; }
QTabBar::tab { background:#122219; padding:10px 18px; color:#a3bcae; border-bottom:2px solid #284337; }
QTabBar::tab:selected { background:#1d3728; border-bottom:2px solid #75dfb8; color:#8ff2c8; }
QCheckBox { spacing:6px; }
QCheckBox::indicator { width:15px; height:15px; border:1px solid #648673; background:#0b1810; border-radius:3px; }
QCheckBox::indicator:checked { background:#75dfb8; border:2px solid #b2ffdc; }
QScrollArea { background:transparent; border:none; }
QScrollBar:vertical { background:#0d1912; width:10px; }
QScrollBar::handle:vertical { background:#34583f; min-height:22px; border-radius:4px; }
QSplitter::handle { background:#223c2c; width:3px; }
"""


class PresetEditor(QMainWindow):
    """PresetEditor(store, catalog, on_changed, resource_root, parent=None).

    ``on_changed()`` is called only after a successful persisted mutation.
    ``refresh()`` rereads store state without mutating it. Close hides the window.
    """

    def __init__(self, store: PlannerStore, catalog: Catalog, on_changed,
                 resource_root: Path | str, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.store = store
        self.catalog = catalog
        self.on_changed = on_changed
        self.resource_root = Path(resource_root)
        self.operator_reference = load_operator_reference(self.resource_root, catalog)
        self._busy = False
        self._icons: dict[str, QIcon] = {}
        # Qt's offscreen platform does not discover Windows fonts automatically.
        # Register an installed font without copying or redistributing it.
        if "Microsoft YaHei UI" not in QFontDatabase.families():
            for installed in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
                if Path(installed).exists():
                    QFontDatabase.addApplicationFont(installed)
        self.setWindowTitle(f"{APP_NAME} · 配队预设")
        self.setWindowIcon(QIcon(str(self.resource_root / ICON_PATH)))
        self.resize(1280, 800)
        self.setMinimumSize(900, 580)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self.refresh()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _button(self, text, callback, primary=False):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _text(text, name=None, wrap=False):
        label = QLabel(text)
        if name:
            label.setObjectName(name)
        label.setWordWrap(wrap)
        return label

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 15, 18, 12)
        outer.setSpacing(12)
        header = QHBoxLayout()
        logo = QLabel()
        pixmap = QPixmap(str(self.resource_root / ICON_PATH))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        header.addWidget(logo)
        title = QVBoxLayout()
        title.addWidget(self._text(f"{APP_NAME} · 配队预设", "title"))
        title.addWidget(self._text("卫戍协议 / 本地配队库", "muted"))
        header.addLayout(title)
        header.addStretch()
        self.enabled = QCheckBox("局内提醒")
        self.enabled.toggled.connect(self._enabled_changed)
        header.addWidget(self.enabled)
        header.addWidget(self._button("导入配队", self._import_dialog))
        header.addWidget(self._button("导出配队", self._export_dialog, True))
        outer.addLayout(header)
        self.status = self._text("", "status", True)
        outer.addWidget(self.status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)
        left = QFrame()
        left.setObjectName("panel")
        left_box = QVBoxLayout(left)
        left_box.addWidget(self._text("预设库", "section"))
        self.preset_list = QListWidget()
        self.preset_list.setMinimumWidth(170)
        self.preset_list.currentItemChanged.connect(self._preset_selected)
        left_box.addWidget(self.preset_list, 1)
        controls = QGridLayout()
        controls.addWidget(self._button("＋ 新建", self._new_preset), 0, 0)
        self.copy_button = self._button("复制", self._duplicate)
        controls.addWidget(self.copy_button, 0, 1)
        self.rename_button = self._button("改名", self._rename)
        controls.addWidget(self.rename_button, 1, 0)
        self.delete_button = self._button("删除", self._delete)
        controls.addWidget(self.delete_button, 1, 1)
        left_box.addLayout(controls)
        left_box.addWidget(self._text("每套最多 20 名目标干员\n全局共用最多 20 名过渡干员\n装备独立保存于每套预设", "muted", True))
        splitter.addWidget(left)

        middle = QWidget()
        middle_box = QVBoxLayout(middle)
        middle_box.setContentsMargins(7, 0, 7, 0)
        self.tabs = QTabBar()
        self.tabs.setExpanding(True)
        for title in ("目标干员", "全局过渡", "目标装备"):
            self.tabs.addTab(title)
        self.tabs.currentChanged.connect(self._mode_changed)
        middle_box.addWidget(self.tabs)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、编号、英文名或职业分支")
        self.search.textChanged.connect(self._refresh_roster)
        search_row.addWidget(self.search, 1)
        self.selected_only = QCheckBox("仅看已选")
        self.selected_only.toggled.connect(self._refresh_roster)
        search_row.addWidget(self.selected_only)
        middle_box.addLayout(search_row)
        filter_row = QHBoxLayout()
        self.profession = QComboBox()
        self.profession.addItem("全部职业", "")
        for value, name in PROFESSIONS.items():
            self.profession.addItem(self._icon(f"assets/profession/{value}.png"), name, value)
        self.profession.currentIndexChanged.connect(self._refresh_roster)
        filter_row.addWidget(self.profession)
        self.bond = QComboBox()
        self.bond.addItem("全部盟约", "")
        for row in self.catalog.bonds:
            self.bond.addItem(self._bond_icon(row["bondId"]), row["name"], row["bondId"])
        self.bond.currentIndexChanged.connect(self._refresh_roster)
        filter_row.addWidget(self.bond)
        self.tier = QComboBox()
        self.tier.addItem("全部装备等阶", 0)
        for tier in range(1, 7):
            self.tier.addItem(f"等阶 {tier}", tier)
        self.tier.currentIndexChanged.connect(self._refresh_roster)
        filter_row.addWidget(self.tier)
        filter_row.addStretch()
        middle_box.addLayout(filter_row)
        self.roster_caption = self._text("", "muted")
        middle_box.addWidget(self.roster_caption)
        self.roster = QListWidget()
        self.roster.setObjectName("roster")
        self.roster.setViewMode(QListWidget.ViewMode.IconMode)
        self.roster.setMovement(QListWidget.Movement.Static)
        self.roster.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.roster.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.roster.setSpacing(4)
        self.roster.setWordWrap(True)
        self.roster.itemChanged.connect(self._roster_changed)
        middle_box.addWidget(self.roster, 1)
        splitter.addWidget(middle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        summary = QFrame()
        summary.setObjectName("panel")
        self.summary_layout = QVBoxLayout(summary)
        self.summary_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(summary)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([205, 735, 310])
        outer.addWidget(self._text("识别画面不会修改配队名单。目录外编号会保留，提醒匹配以当前可识别内容为准。", "muted", True))

    @property
    def mode(self):
        return ("targets", "transitions", "equipment")[self.tabs.currentIndex()]

    def _icon(self, relative):
        if not relative:
            return QIcon()
        if relative not in self._icons:
            path = self.resource_root / relative
            self._icons[relative] = QIcon(str(path)) if path.is_file() else QIcon()
        return self._icons[relative]

    def _bond_icon(self, bond_id):
        icon = self._icon(f"assets/bond/{bond_id}.png")
        if icon.isNull():
            return self._icon(self.catalog.bond_by_id.get(bond_id, {}).get("icon"))
        return icon

    def _mutate(self, action, message="配队已保存。"):
        try:
            result = action()
        except (PlannerError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法保存配队", str(exc))
            self.refresh()
            self.status.setText(str(exc))
            return None
        self.refresh()
        self.status.setText(message)
        if self.on_changed:
            self.on_changed()
        return result

    def refresh(self):
        state = self.store.state
        self._busy = True
        try:
            self.enabled.setChecked(state["enabled"])
            self.preset_list.clear()
            for row in state["presets"]:
                item = QListWidgetItem(f"{row['name']}\n{len(row['targets'])}/20 干员 · {len(row['equipment'])} 装备")
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                self.preset_list.addItem(item)
                if row["id"] == state["activeId"]:
                    self.preset_list.setCurrentItem(item)
            active = self.store.active()
            for button in (self.copy_button, self.rename_button, self.delete_button):
                button.setEnabled(active is not None)
            self.tabs.setTabText(0, f"目标干员 {len(active['targets']) if active else 0}/20")
            self.tabs.setTabText(1, f"全局过渡 {len(state['transitions'])}/20")
            self.tabs.setTabText(2, f"目标装备 {len(active['equipment']) if active else 0}")
            self.status.setText(self.store.load_error or (f"当前预设：{active['name']}  ·  本地保存" if active else "还没有当前预设，请新建或选择预设。"))
            self._render_roster(state)
            self._render_summary(state)
        finally:
            self._busy = False

    def _refresh_roster(self, *_):
        if self._busy:
            return
        self._busy = True
        try:
            self._render_roster(self.store.state)
        finally:
            self._busy = False

    def _mode_changed(self, *_):
        if self._busy:
            return
        self.search.clear()
        self.selected_only.setChecked(False)
        self._refresh_roster()

    def _operator_labels(self, row):
        bonds = [self.catalog.bond_by_id.get(bond, {}).get("name", bond) for bond in row["bonds"]]
        labels = ["天然盟约 · " + " / ".join(bonds) if bonds else "无天然盟约"]
        effects = self.operator_reference.get(row["charId"], {}).get("effectTags", ())
        if effects:
            labels.append("效果标签 · " + " / ".join(effects))
        return labels

    def _render_roster(self, state):
        equipment = self.mode == "equipment"
        self.profession.setVisible(not equipment)
        self.bond.setVisible(not equipment)
        self.tier.setVisible(equipment)
        self.roster.setIconSize(QSize(72, 72) if equipment else QSize(60, 60))
        self.roster.setGridSize(QSize(156, 146) if equipment else QSize(160, 184))
        active = next((p for p in state["presets"] if p["id"] == state["activeId"]), None)
        selected = set(state["transitions"] if self.mode == "transitions" else active[self.mode] if active else [])
        if equipment:
            selected = {self.catalog.resolve_equipment_id(value) for value in selected}
        query = self.search.text().strip().casefold()
        profession = self.profession.currentData()
        bond = self.bond.currentData()
        tier = self.tier.currentData()
        self.roster.clear()
        rows = self.catalog.equipment if equipment else self.catalog.operators
        visible = 0
        for row in rows:
            row_id = row["equipmentId"] if equipment else row["charId"]
            if self.selected_only.isChecked() and row_id not in selected:
                continue
            if equipment and tier and row["tier"] != tier:
                continue
            if not equipment and ((profession and row["profession"] != profession) or (bond and bond not in row["bonds"])):
                continue
            subclass_name = row.get("subclassName") or ""
            search_text = " ".join((row["name"], row_id, row.get("appellation") or "",
                                    (row.get("desc") or "") if equipment else subclass_name)).casefold()
            if query and query not in search_text:
                continue
            detail = f"等阶 {row['tier']}"
            tooltip = [row["name"], row_id]
            if not equipment:
                detail += " · " + PROFESSIONS.get(row["profession"], row["profession"])
                if subclass_name:
                    detail += " · " + subclass_name
                labels = self._operator_labels(row)
                detail += "\n" + "\n".join(labels)
                tooltip.extend(labels)
            else:
                tooltip.append(row.get("desc") or "")
            item = QListWidgetItem(row["name"] + "\n" + detail)
            item.setData(Qt.ItemDataRole.UserRole, row_id)
            item.setToolTip("\n".join(tooltip))
            item.setIcon(self._icon(row.get("icon") if equipment else row.get("avatar")))
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
            if active or self.mode == "transitions":
                flags |= Qt.ItemFlag.ItemIsEnabled
            item.setFlags(flags)
            item.setCheckState(Qt.CheckState.Checked if row_id in selected else Qt.CheckState.Unchecked)
            self.roster.addItem(item)
            visible += 1
        self.roster_caption.setText(f"显示 {visible} {'件装备' if equipment else '名干员'}  ·  " +
                                    ("普通与金色共用提醒" if equipment else "勾选后在局内显示提醒"))

    def _clear_summary(self):
        while self.summary_layout.count():
            item = self.summary_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _summary_group(self, title, ids, kind, remove, hint):
        self.summary_layout.addWidget(self._text(f"{title}  ·  {len(ids)}", "section"))
        self.summary_layout.addWidget(self._text(hint, "muted", True))
        if not ids:
            self.summary_layout.addWidget(self._text("尚未选择", "muted"))
        for value in ids:
            container = QWidget()
            box = QHBoxLayout(container)
            box.setContentsMargins(0, 1, 0, 1)
            if kind == "equipment":
                row = self.catalog.equipment_by_id.get(self.catalog.resolve_equipment_id(value))
            else:
                row = self.catalog.operator_by_id.get(self.catalog.resolve_operator_id(value))
            if row:
                picture = QLabel()
                picture.setPixmap(self._icon(row.get("icon") if kind == "equipment" else row.get("avatar")).pixmap(27, 27))
                box.addWidget(picture)
            text = row["name"] if row else value + "（目录外，已保留）"
            if row and row.get("retired", False):
                text += "（已退场）"
            elif row and kind == "operator":
                text += "\n" + "\n".join(self._operator_labels(row))
            label = self._text(text, wrap=True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setToolTip(value)
            box.addWidget(label, 1)
            button = self._button("×", lambda checked=False, value=value: self._mutate(lambda: remove(value)))
            button.setObjectName("remove")
            button.setToolTip("从名单移除")
            box.addWidget(button)
            self.summary_layout.addWidget(container)
        self.summary_layout.addSpacing(12)

    def _render_summary(self, state):
        self._clear_summary()
        active = next((p for p in state["presets"] if p["id"] == state["activeId"]), None)
        self._summary_group("本套目标", active["targets"] if active else [], "operator",
                            lambda value: self.store.toggle_target(value, False), "最多 20 名，目标优先显示爱心提醒。")
        self._summary_group("本套目标装备", active["equipment"] if active else [], "equipment",
                            lambda value: self.store.toggle_equipment(value, False), "普通与金色共用提醒；不计入天然盟约。")
        self.summary_layout.addWidget(self._text("天然盟约汇总", "section"))
        self.summary_layout.addWidget(self._text("仅统计目标干员自带盟约，实际以上场、精锐与本局规则为准。", "muted", True))
        bonds = self.catalog.bond_summary(active["targets"] if active else [])
        if not bonds:
            self.summary_layout.addWidget(self._text("选择目标干员后显示", "muted"))
        for row in bonds:
            thresholds = " / ".join(str(value) for value in row["thresholds"])
            condition = "需精锐条件" if row["requiresElite"] else "预计达标" if row["active"] else "未达标"
            label = self._text(f"{row['name']}  {row['count']}  ·  {thresholds}  ·  {condition}", wrap=True)
            pixmap = self._bond_icon(row["bondId"]).pixmap(20, 20)
            if pixmap.isNull():
                self.summary_layout.addWidget(label)
            else:
                container = QWidget()
                line = QHBoxLayout(container)
                line.setContentsMargins(0, 0, 0, 0)
                picture = QLabel()
                picture.setPixmap(pixmap)
                line.addWidget(picture)
                line.addWidget(label, 1)
                self.summary_layout.addWidget(container)
        self.summary_layout.addSpacing(15)
        self._summary_group("全局过渡干员", state["transitions"], "operator",
                            lambda value: self.store.toggle_transition(value, False), "所有预设共用；同时属于目标时，目标优先。")
        self.summary_layout.addStretch()

    def _preset_selected(self, current, previous):
        if self._busy or current is None:
            return
        preset_id = current.data(Qt.ItemDataRole.UserRole)
        if preset_id != self.store.state["activeId"]:
            self._mutate(lambda: self.store.select_preset(preset_id), "已切换当前预设。")

    def _roster_changed(self, item):
        if self._busy:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        selected = item.checkState() == Qt.CheckState.Checked
        callback = {"targets": self.store.toggle_target, "transitions": self.store.toggle_transition,
                    "equipment": self.store.toggle_equipment}[self.mode]
        self._mutate(lambda: callback(value, selected))

    def _enabled_changed(self, enabled):
        if not self._busy:
            self._mutate(lambda: self.store.set_enabled(enabled), "局内提醒已开启。" if enabled else "局内提醒已暂停。")

    def _new_preset(self):
        text, ok = QInputDialog.getText(self, "新建配队预设", "预设名称：", text=f"配队 {len(self.store.state['presets']) + 1}")
        if ok:
            self._mutate(lambda: self.store.create_preset(text), "已新建预设。")

    def _duplicate(self):
        self._mutate(self.store.duplicate_preset, "已复制预设。")

    def _rename(self):
        active = self.store.active()
        if not active:
            return
        text, ok = QInputDialog.getText(self, "修改预设名称", "预设名称：", text=active["name"])
        if ok:
            self._mutate(lambda: self.store.rename_preset(active["id"], text), "预设名称已保存。")

    def _delete(self):
        active = self.store.active()
        if active and QMessageBox.question(self, "删除配队预设", f"删除「{active['name']}」？全局过渡名单会保留。",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                           QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._mutate(lambda: self.store.delete_preset(active["id"]), "已删除预设。")

    def _import_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} · 导入配队")
        dialog.resize(700, 530)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._text("导入会追加为新预设，保留现有预设，并切换到导入的配队。", wrap=True))
        text = QTextEdit()
        text.setPlaceholderText("在这里粘贴 stronghold.team-planner JSON")
        file_row = QHBoxLayout()

        def load_file():
            file, _ = QFileDialog.getOpenFileName(dialog, "选择配队文件", "", "JSON 配队文件 (*.json)")
            if file:
                try:
                    if Path(file).stat().st_size > 8 * 1024 * 1024:
                        raise PlannerError("配队文件超过 8 MiB。")
                    text.setPlainText(Path(file).read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError, PlannerError) as exc:
                    QMessageBox.warning(dialog, "无法读取文件", str(exc))

        file_row.addWidget(self._button("选择 JSON 文件", load_file))
        file_row.addWidget(self._button("从剪贴板粘贴", lambda: text.setPlainText(QApplication.clipboard().text())))
        file_row.addStretch()
        layout.addLayout(file_row)
        layout.addWidget(text, 1)
        transitions = QCheckBox("使用文件中的全局过渡名单替换当前名单（最多 20 名）")
        layout.addWidget(transitions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        apply_button = buttons.addButton("追加导入", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(dialog.reject)

        def apply_import():
            try:
                report = self.store.import_json(text.toPlainText(), apply_transitions=transitions.isChecked())
            except (PlannerError, OSError, ValueError) as exc:
                QMessageBox.warning(dialog, "导入失败", str(exc))
                return
            dialog.accept()
            self.refresh()
            self.status.setText(f"已追加导入 {report['imported']} 套预设。 " + " ".join(report["warnings"]))
            if self.on_changed:
                self.on_changed()

        apply_button.clicked.connect(apply_import)
        layout.addWidget(buttons)
        dialog.exec()

    def _export_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} · 导出配队")
        dialog.resize(700, 530)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._text("导出包含目标装备和全局过渡名单，可与网页版互相导入。", wrap=True))
        scope = QComboBox()
        scope.addItem("当前配队", "current")
        scope.addItem("全部配队", "all")
        layout.addWidget(scope)
        text = QTextEdit()
        text.setReadOnly(True)
        layout.addWidget(text, 1)

        def update_text():
            text.setPlainText(self.store.export_json(scope.currentData()))

        scope.currentIndexChanged.connect(update_text)
        update_text()
        row = QHBoxLayout()

        def copy_text():
            QApplication.clipboard().setText(text.toPlainText())
            self.status.setText("配队 JSON 已复制。")

        def save_file():
            scope_name = "当前" if scope.currentData() == "current" else "全部"
            file, _ = QFileDialog.getSaveFileName(dialog, "保存配队文件", f"{APP_NAME}-配队预设-{scope_name}.json", "JSON (*.json)")
            if file:
                try:
                    Path(file).write_text(text.toPlainText(), encoding="utf-8")
                    self.status.setText("配队文件已导出。")
                except OSError as exc:
                    QMessageBox.warning(dialog, "导出失败", str(exc))

        row.addWidget(self._button("复制内容", copy_text))
        row.addWidget(self._button("保存 JSON 文件", save_file, True))
        row.addStretch()
        row.addWidget(self._button("关闭", dialog.accept))
        layout.addLayout(row)
        dialog.exec()


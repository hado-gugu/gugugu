"""Compact, independent operator reference for the current season catalog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from branding import APP_NAME, ICON_PATH
from editor import PROFESSIONS, STYLE
from native import NativeError, get_window_geometry
from operator_reference import load_operator_reference


_WINDOW_FLAGS = Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
_EFFECT_ORDER = ("持续叠加", "单次叠层", "辅助叠层", "战力", "资源", "经济")
_LOOKUP_STYLE = STYLE + """
QPushButton#lookupToggle { padding:3px 8px; }
QPushButton#lookupDetailClose { padding:1px 6px; font-size:14px; }
QListWidget#lookupRoster::item { padding:3px 5px; }
QLabel#lookupName { font-size:17px; font-weight:700; }
QDialog#lookupDetail { border:1px solid #284337; }
"""


def _label(text="", role="", *, wrap=False):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        label.setObjectName(role)
    label.setWordWrap(wrap)
    return label


def _form(row, key):
    forms = row.get("forms")
    form = forms.get(key) if isinstance(forms, dict) else None
    return form if isinstance(form, dict) else {}


def _bond_ids(row):
    values = []
    for source in (row.get("bonds"), _form(row, "normal").get("bondIds"), _form(row, "elite").get("bondIds")):
        if isinstance(source, (list, tuple)):
            values.extend(value for value in source if isinstance(value, str) and value)
    return tuple(dict.fromkeys(values))


def _trait_text(row, key):
    form = _form(row, key)
    desc = form.get("desc")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    lines = []
    for trait in form.get("traits") or []:
        desc = trait.get("desc") if isinstance(trait, dict) else None
        if isinstance(desc, str) and desc.strip():
            lines.append(desc.strip())
    return "\n\n".join(dict.fromkeys(lines)) or "暂无特质资料"


class _OperatorDetail(QDialog):
    def __init__(self, lookup):
        super().__init__(lookup, _WINDOW_FLAGS | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("lookupDetail")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowIcon(lookup.windowIcon())
        self.setStyleSheet(_LOOKUP_STYLE)
        self.resize(308, 350)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 10)
        outer.setSpacing(5)
        title = QHBoxLayout()
        title.addWidget(_label("干员详情", "muted"))
        title.addStretch()
        close_button = QPushButton("×")
        close_button.setObjectName("lookupDetailClose")
        close_button.setAccessibleName("关闭干员详情")
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.close)
        title.addWidget(close_button)
        outer.addLayout(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 5, 0)
        body.setSpacing(7)

        header = QHBoxLayout()
        self.avatar = _label()
        self.avatar.setFixedSize(54, 54)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.avatar)
        info = QVBoxLayout()
        info.setSpacing(3)
        self.name = _label(role="lookupName", wrap=True)
        self.meta = _label(role="muted", wrap=True)
        info.addWidget(self.name)
        info.addWidget(self.meta)
        header.addLayout(info, 1)
        body.addLayout(header)
        body.addWidget(_label("天然盟约", "section"))
        self.tags = QGridLayout()
        self.tags.setHorizontalSpacing(5)
        self.tags.setVerticalSpacing(4)
        self.tags.setColumnStretch(0, 1)
        self.tags.setColumnStretch(1, 1)
        body.addLayout(self.tags)

        self.effects = _label(role="lookupEffectTags", wrap=True)
        self.triggers = _label(role="lookupTriggerTimes", wrap=True)
        self.common_price = _label(role="lookupPurchasePrice", wrap=True)
        for label in (self.effects, self.triggers, self.common_price):
            body.addWidget(label)
            label.hide()

        self.normal_heading = _label("普通特质", "section", wrap=True)
        self.elite_heading = _label("精锐特质", "section", wrap=True)
        self.normal = _label(role="lookupNormalTraits", wrap=True)
        self.elite = _label(role="lookupEliteTraits", wrap=True)
        for heading, label in ((self.normal_heading, self.normal), (self.elite_heading, self.elite)):
            body.addWidget(heading)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.addWidget(label)
        body.addStretch()
        self.scroll = scroll

    def set_operator(self, row, bond_ids, bonds, icon_for, reference):
        name = row.get("name") or row.get("charId") or "未命名干员"
        self.setWindowTitle(f"{APP_NAME} · {name}")
        self.name.setText(name)
        icon = icon_for(row.get("avatar"))
        if icon.isNull():
            self.avatar.setText(name[0])
        else:
            self.avatar.setPixmap(icon.pixmap(self.avatar.size(), self.devicePixelRatioF()))

        profession = row.get("profession") or ""
        profession_name = row.get("professionName") or PROFESSIONS.get(profession, profession)
        specialty = " / ".join(filter(None, (profession_name, row.get("subclassName"))))
        tier = row.get("tier")
        self.meta.setText(" · ".join(filter(None, (f"{tier} 阶" if tier else "", specialty))) or "暂无职业或等阶资料")

        while self.tags.count():
            self.tags.takeAt(0).widget().deleteLater()
        if not bond_ids:
            self.tags.addWidget(_label("无天然盟约", "muted"), 0, 0, 1, 2)
        for index, bond_id in enumerate(bond_ids):
            bond = bonds.get(bond_id, {})
            tag = QFrame()
            tag.setObjectName("panel")
            tag.setToolTip("核心盟约" if bond.get("isCore") else "其他盟约")
            layout = QHBoxLayout(tag)
            layout.setContentsMargins(5, 3, 5, 3)
            layout.setSpacing(4)
            icon = icon_for(bond.get("icon"))
            if not icon.isNull():
                image = _label()
                image.setPixmap(icon.pixmap(QSize(15, 15), self.devicePixelRatioF()))
                layout.addWidget(image)
            layout.addWidget(_label(bond.get("name") or bond_id, wrap=True), 1)
            self.tags.addWidget(tag, index // 2, index % 2)
        for label, title, key in ((self.effects, "效果标签", "effectTags"),
                                  (self.triggers, "触发时机", "triggerTimes")):
            values = reference.get(key, ())
            label.setText(title + "  " + " · ".join(values) if values else "")
            label.setVisible(bool(values))

        normal_price = _form(row, "normal").get("purchasePrice")
        elite_price = _form(row, "elite").get("purchasePrice")
        common = type(normal_price) is int and type(elite_price) is int and normal_price == elite_price
        self.common_price.setText(f"普通 / 精锐购买价  {normal_price}" if common else "")
        self.common_price.setVisible(common)
        self.normal_heading.setText(f"普通特质 · 购买价 {normal_price}"
                                     if type(normal_price) is int and not common else "普通特质")
        self.elite_heading.setText(f"精锐特质 · 购买价 {elite_price}"
                                    if type(elite_price) is int and not common else "精锐特质")
        self.normal.setText(_trait_text(row, "normal"))
        self.elite.setText(_trait_text(row, "elite"))
        self.scroll.verticalScrollBar().setValue(0)


class OperatorLookup(QDialog):
    """OperatorLookup(catalog, resource_root) opens a non-modal tool window."""

    def __init__(self, catalog, resource_root):
        super().__init__(None, _WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle(f"{APP_NAME} · 干员速查")
        self._root = Path(resource_root)
        self.setWindowIcon(QIcon(str(self._root / ICON_PATH)))
        self.setStyleSheet(_LOOKUP_STYLE)
        self._icons = {}
        self._rows = {key: row for row in catalog.operators
                      if (key := row.get("charId") or row.get("id"))}
        self._bonds = {row["bondId"]: row for row in catalog.bonds if row.get("bondId")}
        self._operator_bonds = {key: _bond_ids(row) for key, row in self._rows.items()}
        self._reference = load_operator_reference(self._root, catalog)
        self._selected = None
        self._expanded_height = 390
        self._collapsed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)
        header = QHBoxLayout()
        header.addWidget(_label("干员速查", "section"))
        self.count = _label(role="muted")
        header.addWidget(self.count)
        header.addStretch()
        self.toggle = QPushButton("收起")
        self.toggle.setObjectName("lookupToggle")
        self.toggle.clicked.connect(self._toggle_fold)
        header.addWidget(self.toggle)
        layout.addLayout(header)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)
        self.core_filter = self._bond_filter(True, "lookupCoreBond")
        self.other_filter = self._bond_filter(False, "lookupOtherBond")
        self.effect_filter = self._reference_filter("effectTags", "lookupEffectFilter", _EFFECT_ORDER)
        self.tier_filter = QComboBox()
        self.tier_filter.setObjectName("lookupTierFilter")
        self.tier_filter.addItem("全部", None)
        for tier in range(1, 7):
            self.tier_filter.addItem(f"{tier} 阶", tier)
        self.trigger_filter = self._reference_filter("triggerTimes", "lookupTriggerFilter")
        body.addLayout(self._filter_grid((
            ("核心盟约", self.core_filter), ("其他盟约", self.other_filter),
            ("效果标签", self.effect_filter), ("触发时机", self.trigger_filter),
        )))
        tier_row = QHBoxLayout()
        tier_label = _label("阶位", "muted")
        tier_label.setBuddy(self.tier_filter)
        tier_row.addWidget(tier_label)
        tier_row.addWidget(self.tier_filter, 1)
        body.addLayout(tier_row)

        self.search = QLineEdit()
        self.search.setObjectName("lookupSearch")
        self.search.setPlaceholderText("搜索干员名称")
        self.search.setClearButtonEnabled(True)
        body.addWidget(self.search)

        self.roster = QListWidget()
        self.roster.setObjectName("lookupRoster")
        self.roster.setIconSize(QSize(25, 25))
        self.roster.setUniformItemSizes(True)
        body.addWidget(self.roster, 1)
        credit = _label("感谢海马提供的标签整理（小红书id：11543288921）", "muted", wrap=True)
        credit.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(credit)
        layout.addWidget(self.body, 1)

        self.detail = _OperatorDetail(self)
        for combo in (self.core_filter, self.other_filter, self.effect_filter, self.tier_filter, self.trigger_filter):
            combo.currentIndexChanged.connect(self._refresh)
        self.search.textChanged.connect(self._refresh)
        self.roster.itemClicked.connect(self._open_detail)
        self.roster.itemActivated.connect(self._open_detail)
        self.resize(270, self._expanded_height)
        self._refresh()

    def _icon(self, path):
        if not isinstance(path, str) or not path:
            return QIcon()
        if path not in self._icons:
            self._icons[path] = QIcon(str(self._root / path))
        return self._icons[path]

    def _bond_filter(self, core, name):
        combo = QComboBox()
        combo.setObjectName(name)
        combo.addItem("全部", None)
        for bond_id, row in self._bonds.items():
            if bool(row.get("isCore")) == core:
                combo.addItem(self._icon(row.get("icon")), row.get("name") or bond_id, bond_id)
        return combo

    @staticmethod
    def _filter_grid(controls):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)
        for index, (text, combo) in enumerate(controls):
            row, column = divmod(index, 2)
            label = _label(text, "muted")
            label.setBuddy(combo)
            grid.addWidget(label, row * 2, column)
            grid.addWidget(combo, row * 2 + 1, column)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return grid

    def _reference_filter(self, key, name, order=()):
        combo = QComboBox()
        combo.setObjectName(name)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(5)
        combo.addItem("全部", None)
        labels = {value for char_id in self._rows for value in self._reference.get(char_id, {}).get(key, ())}
        values = [value for value in order if value in labels]
        values.extend(sorted(labels.difference(order)))
        for value in values:
            combo.addItem(value, value)
        return combo

    def _refresh(self, *_):
        filters = (self.core_filter.currentData(), self.other_filter.currentData())
        effect = self.effect_filter.currentData()
        selected_tier = self.tier_filter.currentData()
        trigger = self.trigger_filter.currentData()
        query = self.search.text().strip().casefold()
        self.roster.clear()
        selected_item = None
        for key, row in self._rows.items():
            tier = row.get("tier")
            if selected_tier is not None and tier != selected_tier:
                continue
            if any(bond and bond not in self._operator_bonds[key] for bond in filters):
                continue
            reference = self._reference.get(key, {})
            if effect and effect not in reference.get("effectTags", ()):
                continue
            if trigger and trigger not in reference.get("triggerTimes", ()):
                continue
            name = row.get("name") or key
            appellation = row.get("appellation") or ""
            if query and query not in name.casefold() and query not in appellation.casefold():
                continue
            item = QListWidgetItem(self._icon(row.get("avatar")), f"{name}  ·  {tier} 阶" if tier else name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.roster.addItem(item)
            if key == self._selected:
                selected_item = item
        self.count.setText(f"{self.roster.count()} 名")
        if selected_item is None:
            self._selected = None
            self.detail.hide()
        else:
            self.roster.setCurrentItem(selected_item)

    def _toggle_fold(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._expanded_height = self.height()
            self.detail.hide()
            self.body.hide()
            self.toggle.setText("展开")
            self.layout().activate()
            self.setFixedHeight(self.sizeHint().height())
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.body.show()
            self.toggle.setText("收起")
            self.resize(self.width(), self._expanded_height)

    def _open_detail(self, item):
        if self._collapsed:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        row = self._rows.get(key)
        if row is None:
            return
        if self._selected != key or not self.detail.isVisible():
            self.detail.set_operator(row, self._operator_bonds[key], self._bonds, self._icon,
                                     self._reference.get(key, {}))
        self._selected = key
        self._place_detail()
        self.detail.show()
        self._place_detail()

    def _place_detail(self):
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self._visible_frame()
        width, height = self.detail.width(), self.detail.height()
        right = frame.right() + 1
        left = frame.left() - width
        space_on_right = available.right() - frame.right()
        space_on_left = frame.left() - available.left()
        x = right if space_on_right >= width or space_on_right >= space_on_left else left
        x = max(available.left(), min(x, available.right() - width + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - height + 1))
        self.detail.move(x, y)

    def _visible_frame(self):
        frame = self.frameGeometry()
        if QGuiApplication.platformName() != "windows" or not self.isVisible():
            return frame
        try:
            geometry = get_window_geometry(int(self.winId()))
        except NativeError:
            return frame
        window = geometry["window_rect"]
        visible = geometry["frame_rect"]
        scale = self.devicePixelRatioF()
        # Qt uses logical coordinates. Trim only when its frame includes the
        # Win32 invisible resize borders, without remapping monitor origins.
        if (abs(frame.width() * scale - (window[2] - window[0])) > 2 * scale
                or abs(frame.height() * scale - (window[3] - window[1])) > 2 * scale):
            return frame
        insets = (visible[0] - window[0], visible[1] - window[1],
                  window[2] - visible[2], window[3] - visible[3])
        if not all(0 <= inset <= 16 * scale for inset in insets):
            return frame
        left, top, right, bottom = (round(inset / scale) for inset in insets)
        return frame.adjusted(left, top, -right, -bottom)

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.detail.isVisible():
            self._place_detail()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.detail.isVisible():
            self._place_detail()

    def hideEvent(self, event):
        self.detail.hide()
        super().hideEvent(event)

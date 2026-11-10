"""Render the tutorial's static diagrams without starting the assistant."""

from functools import lru_cache
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1] / "assets"
OUTPUT = ROOT / "tutorial"
FONT = Path(r"C:\Windows\Fonts\msyh.ttc")
BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
SCALE = 2
BG = "#0d1712"
PANEL = "#14231c"
CONTROL = "#203329"
STROKE = "#365346"
LIGHT = "#eaf7ee"
MUTED = "#a7beb0"
MINT = "#70ebc1"
DARK_TEXT = "#0e2018"


def scaled(values):
    return tuple(round(value * SCALE) for value in values)


@lru_cache
def font(size, bold):
    return ImageFont.truetype(str(BOLD if bold else FONT), size * SCALE)


class Diagram:
    def __init__(self, height=380):
        self.height = height
        self.image = Image.new("RGB", (1000 * SCALE, height * SCALE), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.rect((1, 1, 998, height - 2), BG, "#2a4236", 18)

    def rect(self, box, fill, outline=None, radius=8, width=2):
        self.draw.rounded_rectangle(scaled(box), radius * SCALE, fill=fill, outline=outline, width=width * SCALE)

    def text(self, point, text, size=26, color=LIGHT, bold=False, anchor="lm"):
        self.draw.text(scaled(point), text, fill=color, font=font(size, bold), anchor=anchor)

    def line(self, points, fill=MINT, width=3):
        self.draw.line([scaled(point) for point in points], fill=fill, width=width * SCALE, joint="curve")

    def arrow(self, points, fill=MINT):
        self.line(points, fill)
        x0, y0 = points[-2]
        x1, y1 = points[-1]
        angle = math.atan2(y1 - y0, x1 - x0)
        head = [(x1, y1)] + [(x1 - 12 * math.cos(angle + offset), y1 - 12 * math.sin(angle + offset))
                               for offset in (-.54, .54)]
        self.draw.polygon([scaled(point) for point in head], fill=fill)

    def icon(self, path, box):
        left, top, right, bottom = scaled(box)
        with Image.open(ROOT / path) as source:
            image = ImageOps.contain(source.convert("RGBA"), (right - left, bottom - top), Image.Resampling.LANCZOS)
        self.image.paste(image, (left + (right - left - image.width) // 2,
                                 top + (bottom - top - image.height) // 2), image)

    def button(self, box, text, primary=False, size=25):
        self.rect(box, MINT if primary else CONTROL, MINT if primary else STROKE)
        left, top, right, bottom = box
        self.text(((left + right) / 2, (top + bottom) / 2), text, size,
                  DARK_TEXT if primary else LIGHT, primary, "mm")

    def check(self, x, y):
        self.rect((x, y, x + 24, y + 24), MINT, radius=5)
        self.line([(x + 5, y + 12), (x + 10, y + 17), (x + 19, y + 7)], DARK_TEXT, 3)

    def step(self, x, y, number, title, note):
        self.rect((x, y - 19, x + 38, y + 19), "#244437", MINT, 18)
        self.text((x + 19, y), str(number), 22, MINT, True, "mm")
        self.text((x + 54, y), title, 28, LIGHT, True)
        self.text((x + 54, y + 40), note, 23, MUTED)

    def save(self, name):
        self.image.resize((1000, self.height), Image.Resampling.LANCZOS).save(OUTPUT / f"{name}.png", optimize=True)


def bind():
    d = Diagram(362)
    d.rect((38, 34, 559, 328), PANEL, STROKE, 13)
    d.icon("app-icon.png", (59, 52, 102, 95))
    d.text((116, 74), "咕顾谷", 32, LIGHT, True)
    d.line([(59, 109), (537, 109)], STROKE, 2)
    d.text((62, 137), "游戏 / 网页所在窗口", 24, MUTED)
    d.rect((61, 160, 432, 219), CONTROL, MINT)
    d.text((81, 189), "明日方舟", 29, LIGHT, True)
    d.line([(394, 185), (403, 193), (412, 185)], MINT)
    d.button((444, 160, 535, 219), "刷新")
    d.button((61, 242, 535, 302), "绑定所选窗口", True, 29)
    d.step(635, 127, 1, "选择游戏窗口", "列表中找到「明日方舟」")
    d.step(635, 249, 2, "确认绑定", "点击「绑定所选窗口」")
    d.arrow([(623, 144), (413, 144), (413, 156)])
    d.arrow([(623, 265), (542, 265)])
    d.save("bind")


def roster_entry(d, x, name, art):
    d.rect((x, 204, x + 242, 300), CONTROL, "#5a9b7e", 10)
    d.rect((x + 11, 217, x + 77, 284), "#102218", radius=9)
    d.icon(art, (x + 11, 217, x + 77, 284))
    d.text((x + 91, 251), name, 25, LIGHT, True)
    d.check(x + 204, 240)


def preset():
    d = Diagram(368)
    d.rect((35, 34, 965, 334), PANEL, STROKE, 13)
    d.icon("app-icon.png", (54, 50, 100, 96))
    d.text((113, 74), "咕顾谷 · 配队预设", 30, LIGHT, True)
    d.rect((645, 51, 785, 96), "#1b392c", MINT)
    d.check(655, 62)
    d.text((688, 74), "局内提醒", 23, MINT, True)
    d.button((801, 51, 943, 96), "导入配队", size=23)
    d.line([(55, 115), (943, 115)], STROKE, 2)
    d.rect((56, 132, 260, 311), "#112019", STROKE)
    d.text((73, 155), "预设库", 23, MINT, True)
    d.rect((71, 175, 243, 227), "#254637", "#5a9b7e")
    d.text((88, 201), "配队 1", 25, LIGHT, True)
    d.button((71, 241, 243, 293), "＋ 新建", size=23)
    d.button((279, 132, 493, 181), "目标干员", True, 25)
    d.button((502, 132, 714, 181), "全局过渡", size=25)
    d.button((723, 132, 941, 181), "目标装备", size=25)
    roster_entry(d, 281, "霜叶", "char/avatar/char_193_frostl.png")
    roster_entry(d, 535, "折桠", "char/avatar/char_4207_branch.png")
    d.text((795, 230), "勾选要提醒", 23, MUTED)
    d.text((795, 265), "的干员或装备", 23, MUTED)
    d.save("preset")


def product(d, box, name, art=None, marker=None, compact=False):
    left, top, right, bottom = box
    d.rect(box, "#162a22", "#547866", 9)
    margin = 10 if compact else 14
    name_height = 36 if compact else 47
    d.rect((left + 2, bottom - name_height, right - 2, bottom - 2), "#285143", radius=6)
    if art is not None:
        d.icon(art, (left + margin, top + margin, right - margin, bottom - name_height - 7))
    d.text(((left + right) / 2, bottom - name_height / 2 - 1), name, 23 if compact else 26, LIGHT, True, "mm")
    if marker:
        d.icon(f"markers/{marker}.png", (right - 66, top - 15, right - 6, top + 45))


def scan():
    d = Diagram(372)
    d.rect((38, 38, 350, 334), PANEL, STROKE, 12)
    d.icon("app-icon.png", (57, 55, 103, 101))
    d.text((116, 78), "咕顾谷", 29, LIGHT, True)
    d.text((58, 132), "当前配队预设", 23, MUTED)
    d.rect((58, 153, 329, 203), CONTROL, STROKE)
    d.text((75, 178), "配队 1", 24)
    d.button((58, 231, 329, 294), "开启扫描", True, 29)
    d.arrow([(363, 262), (415, 262)])
    d.text((440, 57), "展开商店后查看提醒", 29, LIGHT, True)
    product(d, (441, 105, 658, 292), "霜叶", "char/avatar/char_193_frostl.png", "target")
    product(d, (693, 105, 910, 292), "幽灵鲨", "char/avatar/char_143_ghost.png", "transition")
    d.text((549, 324), "爱心 · 目标", 24, MINT, True, "mm")
    d.text((801, 324), "点赞 · 过渡", 24, MINT, True, "mm")
    d.save("scan")


def calibrate():
    d = Diagram(405)
    d.rect((34, 23, 967, 382), PANEL, STROKE, 13)
    d.text((57, 54), "咕顾谷 · 框选商店", 28, LIGHT, True)
    d.rect((53, 80, 949, 335), "#101d18", STROKE)
    for row in range(2):
        for col in range(10):
            x = 77 + col * 59
            y = 97 + row * 41
            shade = "#253a32" if (col + row) % 2 else "#2d4138"
            d.rect((x, y, x + 56, y + 38), shade, "#365045", 3)
    d.text((89, 131), "战场留在框外", 24, MUTED, True)
    d.button((697, 105, 801, 151), "冻结", size=22)
    d.button((816, 105, 920, 151), "刷新", size=22)
    d.text((698, 178), "按钮留在框外", 22, MUTED)

    d.rect((63, 192, 937, 327), "#143127", MINT, 6, 3)
    for x, y, sx, sy in ((63, 192, 1, 1), (937, 192, -1, 1), (63, 327, 1, -1), (937, 327, -1, -1)):
        d.line([(x, y + 15 * sy), (x, y), (x + 15 * sx, y)], MINT, 5)
    d.text((125, 246), "预留", 23, MUTED, True, "mm")
    d.text((125, 280), "扩槽空间", 20, MUTED, False, "mm")
    d.rect((184, 204, 312, 315), "#263931", "#658071", 8)
    d.text((248, 237), "I", 35, LIGHT, True, "mm")
    d.text((248, 284), "升级", 23, MINT, True, "mm")
    products = [("雪祀之衣", "item/trap_1127_acarm127.png"), ("霜叶", "char/avatar/char_193_frostl.png"),
                ("幽灵鲨", "char/avatar/char_143_ghost.png"), ("折桠", "char/avatar/char_4207_branch.png")]
    for index, (name, art) in enumerate(products):
        x = 323 + index * 152
        product(d, (x, 204, x + 145, 315), name, art, compact=True)
    d.text((59, 357), "框住升级和完整的商品卡片，向左留出扩槽空间", 24, MINT, True)
    d.button((796, 339, 949, 374), "保存范围", True, 23)
    d.save("calibrate")


if __name__ == "__main__":
    OUTPUT.mkdir(exist_ok=True)
    for render in (bind, preset, scan, calibrate):
        render()

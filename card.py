"""Генерація сезонної (зима/весна/літо/осінь) картинки з графіком відключень (Pillow)."""
import io
import math
import os
import random
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W = 1080
PAD = 48

_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_REG = [
    os.path.join(_HERE, "fonts", "regular.ttf"),
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_BOLD = [
    os.path.join(_HERE, "fonts", "bold.ttf"),
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

BULB_COLORS = [(255, 82, 82), (255, 200, 60), (90, 220, 120), (90, 170, 255)]


def _font(paths, size):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _segments(intervals):
    segs = []
    for iv in intervals:
        try:
            a, b = iv.split("–")
            s, e = _to_min(a), _to_min(b)
        except ValueError:
            continue
        if e == 0 or e < s:
            e = 1440
        if e > s:
            segs.append((s, e))
    return segs


def _total_minutes(intervals) -> int:
    return sum(e - s for s, e in _segments(intervals))


def _fmt_dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} год {m} хв"
    if h:
        return f"{h} год"
    return f"{m} хв"


# ---------------------------------------------------------------- Сезонні теми
# Сезон змінюється автоматично за датою (за замовчуванням — 1 грудня / 1 березня / 1 червня / 1 вересня).
# Примусово: змінна середовища THEME=winter|spring|summer|autumn.
SEASON_STARTS = [(3, 1, "spring"), (6, 1, "summer"), (9, 1, "autumn"), (12, 1, "winter")]

THEMES = {
    "winter": {
        "name": "winter", "ua": "зимова", "top": (8, 18, 46), "bottom": (22, 58, 98),
        "accent": (255, 214, 70, 255), "tint": (170, 205, 240, 255), "tint2": (150, 185, 225, 255),
        "tint3": (200, 220, 240, 255), "bokeh": (255, 255, 255, 70), "greeting": "Теплої зими!",
    },
    "spring": {
        "name": "spring", "ua": "весняна", "top": (14, 42, 72), "bottom": (40, 112, 122),
        "accent": (255, 190, 215, 255), "tint": (190, 225, 235, 255), "tint2": (165, 205, 215, 255),
        "tint3": (215, 238, 240, 255), "bokeh": (255, 200, 225, 60), "greeting": "Гарної весни!",
    },
    "summer": {
        "name": "summer", "ua": "літня", "top": (10, 58, 118), "bottom": (24, 120, 160),
        "accent": (255, 208, 64, 255), "tint": (185, 225, 245, 255), "tint2": (160, 205, 230, 255),
        "tint3": (210, 235, 248, 255), "bokeh": (255, 232, 150, 60), "greeting": "Гарного літа!",
    },
    "autumn": {
        "name": "autumn", "ua": "осіння", "top": (38, 18, 14), "bottom": (112, 54, 26),
        "accent": (255, 166, 64, 255), "tint": (245, 212, 175, 255), "tint2": (225, 185, 150, 255),
        "tint3": (250, 228, 205, 255), "bokeh": (255, 170, 80, 55), "greeting": "Затишної осені!",
    },
}


def current_theme(now: datetime | None = None, override: str | None = None) -> str:
    """Назва теми: з параметра/змінної THEME, інакше за датою."""
    name = (override or os.getenv("THEME", "")).strip().lower()
    if name in THEMES:
        return name
    now = now or datetime.now()
    cur = "winter"  # з 1 грудня і до 28/29 лютого (до початку весни)
    for m, d, n in SEASON_STARTS:
        if (now.month, now.day) >= (m, d):
            cur = n
    return cur


def _greeting(th: dict, now: datetime) -> str:
    if th["name"] == "winter" and (now.month == 12 or (now.month == 1 and now.day <= 15)):
        return "Гарних і світлих свят!"
    return th["greeting"]


def _rot_ellipse(cx, cy, rx, ry, ang, n=14):
    ca, sa = math.cos(ang), math.sin(ang)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + x * ca - y * sa, cy + x * sa + y * ca))
    return pts


def _leaf(d, cx, cy, size, ang, color, vein=True):
    """Листок-«мигдалина» довжиною 2*size, повернутий на ang (радіани)."""
    n = 10
    ca, sa = math.cos(ang), math.sin(ang)
    prof = [(-size + 2 * size * i / n, size * 0.5 * math.sin(math.pi * i / n)) for i in range(n + 1)]
    pts = prof + [(x, -w) for x, w in reversed(prof)]
    d.polygon([(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts], fill=color)
    if vein:
        vc = tuple(int(c * 0.65) for c in color[:3]) + (color[3],)
        d.line([(cx - size * ca * 0.9, cy - size * sa * 0.9), (cx + size * ca * 0.9, cy + size * sa * 0.9)], fill=vc, width=1)


def _sparkle(d, x, y, r, color):
    pts = []
    for k in range(8):
        a = math.radians(k * 45)
        rr = r if k % 2 == 0 else r * 0.28
        pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
    d.polygon(pts, fill=color)


def _blossom(d, x, y, r, color):
    for k in range(5):
        a = math.radians(k * 72 - 90)
        px_, py_ = x + r * 0.75 * math.cos(a), y + r * 0.75 * math.sin(a)
        d.ellipse([px_ - r * 0.55, py_ - r * 0.55, px_ + r * 0.55, py_ + r * 0.55], fill=color)
    d.ellipse([x - r * 0.3, y - r * 0.3, x + r * 0.3, y + r * 0.3], fill=(255, 205, 70, 255))


def _background(width: int, height: int, seed: int, th: dict) -> Image.Image:
    bg = Image.new("RGB", (width, height))
    px = ImageDraw.Draw(bg)
    top, bottom = th["top"], th["bottom"]
    for y in range(height):
        t = y / max(1, height - 1)
        px.line([(0, y), (width, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    rnd = random.Random(seed)
    parts = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sd = ImageDraw.Draw(parts)
    kind = th["name"]
    if kind == "winter":  # сніг
        for _ in range(int(height / 7)):
            x, y = rnd.randint(0, width), rnd.randint(0, height)
            r = rnd.choice([1, 1, 2, 2, 3, 4])
            a = rnd.randint(90, 230)
            sd.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    elif kind == "spring":  # пелюстки
        palette = [(255, 183, 208), (255, 214, 229), (255, 255, 255), (250, 160, 190)]
        for _ in range(int(height / 20)):
            x, y = rnd.randint(0, width), rnd.randint(0, height)
            s = rnd.randint(5, 10)
            sd.polygon(_rot_ellipse(x, y, s, s * 0.55, rnd.uniform(0, math.pi)), fill=rnd.choice(palette) + (rnd.randint(150, 230),))
    elif kind == "summer":  # сонячні відблиски
        for _ in range(int(height / 14)):
            x, y = rnd.randint(0, width), rnd.randint(0, height)
            if rnd.random() < 0.3:
                _sparkle(sd, x, y, rnd.randint(5, 9), (255, 244, 190, rnd.randint(120, 220)))
            else:
                r = rnd.choice([1, 2, 2, 3])
                sd.ellipse([x - r, y - r, x + r, y + r], fill=(255, 248, 210, rnd.randint(90, 200)))
    else:  # осіннє листя
        palette = [(230, 120, 30), (200, 60, 30), (240, 170, 50), (150, 70, 30)]
        for _ in range(int(height / 38)):
            x, y = rnd.randint(0, width), rnd.randint(0, height)
            _leaf(sd, x, y, rnd.randint(8, 15), rnd.uniform(0, 2 * math.pi), rnd.choice(palette) + (rnd.randint(190, 240),))

    big = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bd = ImageDraw.Draw(big)
    for _ in range(14):
        x, y = rnd.randint(0, width), rnd.randint(0, height)
        r = rnd.randint(5, 9)
        bd.ellipse([x - r, y - r, x + r, y + r], fill=th["bokeh"])
    big = big.filter(ImageFilter.GaussianBlur(3))
    bg = Image.alpha_composite(bg.convert("RGBA"), big)
    return Image.alpha_composite(bg, parts)


def _decor_top(img: Image.Image, th: dict) -> Image.Image:
    """Верхня прикраса: гірлянда / квітуча гілка / прапорці / осіння гілка."""
    w = img.size[0]
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    d = ImageDraw.Draw(img)
    kind = th["name"]

    if kind == "winter":
        pts = [(x, 30 + 22 * math.sin(x / 90)) for x in range(-10, w + 20, 6)]
        d.line(pts, fill=(40, 70, 60, 255), width=4)
        for i, x in enumerate(range(20, w, 64)):
            y = 30 + 22 * math.sin(x / 90)
            c = BULB_COLORS[i % len(BULB_COLORS)]
            gd.ellipse([x - 20, y + 4, x + 20, y + 44], fill=c + (120,))
            d.line([(x, y), (x, y + 10)], fill=(40, 70, 60, 255), width=3)
            d.ellipse([x - 8, y + 10, x + 8, y + 34], fill=c + (255,))
            d.ellipse([x - 4, y + 14, x, y + 20], fill=(255, 255, 255, 190))

    elif kind == "spring":
        rnd = random.Random(11)
        greens = [(110, 190, 110, 255), (150, 210, 120, 255), (90, 170, 120, 255)]
        petals = [(255, 183, 208, 255), (255, 225, 235, 255), (250, 150, 185, 255)]
        d.line([(x, 28 + 20 * math.sin(x / 110)) for x in range(-10, w + 20, 6)], fill=(96, 64, 48, 255), width=6)
        for i, x in enumerate(range(10, w, 44)):
            y = 28 + 20 * math.sin(x / 110)
            side = 1 if i % 2 == 0 else -1
            _leaf(d, x + 8, y + side * 14, 15, math.radians(side * 55 + rnd.randint(-15, 15)), greens[i % 3])
        for i, x in enumerate(range(40, w, 118)):
            y = 28 + 20 * math.sin(x / 110)
            c = petals[i % 3]
            d.line([(x, y), (x, y + 12)], fill=(96, 64, 48, 255), width=3)
            gd.ellipse([x - 28, y + 6, x + 28, y + 62], fill=c[:3] + (90,))
            _blossom(d, x, y + 24, 15, c)

    elif kind == "summer":
        cols = [(255, 200, 60, 255), (255, 112, 112, 255), (255, 255, 255, 255), (64, 224, 208, 255)]
        f = lambda x: 22 + 16 * math.sin(x / 120)
        d.line([(x, f(x)) for x in range(-10, w + 20, 6)], fill=(235, 235, 235, 255), width=3)
        for i, x in enumerate(range(24, w, 62)):
            d.polygon([(x - 22, f(x - 22)), (x + 22, f(x + 22)), (x, f(x) + 48)], fill=cols[i % 4])

    else:  # autumn
        rnd = random.Random(5)
        cols = [(230, 120, 30, 255), (200, 60, 30, 255), (240, 170, 50, 255), (170, 90, 35, 255)]
        f = lambda x: 26 + 18 * math.sin(x / 100)
        d.line([(x, f(x)) for x in range(-10, w + 20, 6)], fill=(84, 52, 34, 255), width=6)
        for i, x in enumerate(range(24, w, 52)):
            y = f(x)
            d.line([(x, y), (x, y + 10)], fill=(84, 52, 34, 255), width=2)
            _leaf(d, x, y + 28, 19, math.radians(90 + rnd.randint(-25, 25)), cols[i % 4])

    glow = glow.filter(ImageFilter.GaussianBlur(9))
    return Image.alpha_composite(img, glow)


def _tree(layer: Image.Image, cx: int, base_y: int, scale: float = 1.0):
    d = ImageDraw.Draw(layer)
    tiers = [(150, 80), (125, 70), (100, 62)]
    y = base_y
    d.rectangle([cx - 12 * scale, y, cx + 12 * scale, y + 24 * scale], fill=(110, 70, 40, 255))
    for i, (w, h) in enumerate(tiers):
        w, h = w * scale, h * scale
        d.polygon([(cx - w / 2, y), (cx + w / 2, y), (cx, y - h)], fill=(30, 140 + i * 18, 80, 255))
        y -= h * 0.62
    star_y = y - 4
    d.polygon(
        [(cx + 26 * scale * math.sin(math.radians(a * 36)) * (1 if a % 2 == 0 else 0.45),
          star_y - 26 * scale * math.cos(math.radians(a * 36)) * (1 if a % 2 == 0 else 0.45))
         for a in range(10)],
        fill=(255, 214, 70, 255),
    )
    rnd = random.Random(7)
    for k in range(9):
        bx = cx + rnd.randint(-48, 48) * scale
        by = base_y - rnd.randint(10, 160) * scale
        c = BULB_COLORS[k % 4]
        d.ellipse([bx - 6, by - 6, bx + 6, by + 6], fill=c + (255,))


def _tulips(layer, cx, base_y, s):
    d = ImageDraw.Draw(layer)
    for dx, h, col, light in [(-44, 95, (240, 80, 110, 255), (255, 125, 150, 255)),
                              (0, 140, (255, 196, 60, 255), (255, 224, 110, 255)),
                              (44, 110, (230, 100, 190, 255), (250, 150, 215, 255))]:
        sx, top_y = cx + dx * s, base_y - h * s
        d.line([(sx, base_y + 22 * s), (sx, top_y)], fill=(70, 170, 90, 255), width=max(3, int(6 * s)))
        side = -1 if dx <= 0 else 1
        _leaf(d, sx + side * 16 * s, base_y - h * 0.3 * s, 36 * s, math.radians(-60 * side), (70, 170, 90, 255))
        r = 21 * s
        d.ellipse([sx - r, top_y - r * 1.1, sx + r, top_y + r * 1.1], fill=col)
        d.polygon([(sx - r, top_y - r * 0.2), (sx - r * 0.95, top_y - r * 1.6), (sx - r * 0.35, top_y - r * 0.75),
                   (sx, top_y - r * 1.8), (sx + r * 0.35, top_y - r * 0.75), (sx + r * 0.95, top_y - r * 1.6),
                   (sx + r, top_y - r * 0.2)], fill=light)


def _sun(layer, cx, base_y, s):
    d = ImageDraw.Draw(layer)
    cy, r = base_y - 85 * s, 44 * s
    for k, a in enumerate((34, 48, 70)):
        rr = r + (46 - k * 14) * s
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(255, 220, 100, a))
    for k in range(16):
        ang = math.radians(k * 22.5)
        length = (34 if k % 2 == 0 else 22) * s
        w = 7 * s
        p0, p1 = r + 8 * s, r + 8 * s + length
        ca, sa = math.cos(ang), math.sin(ang)
        d.polygon([(cx + p0 * ca - w * sa, cy + p0 * sa + w * ca), (cx + p0 * ca + w * sa, cy + p0 * sa - w * ca),
                   (cx + p1 * ca, cy + p1 * sa)], fill=(255, 214, 70, 255))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 196, 48, 255))
    d.ellipse([cx - r * 0.78, cy - r * 0.78, cx + r * 0.78, cy + r * 0.78], fill=(255, 224, 108, 255))


def _pumpkin(layer, cx, base_y, s):
    d = ImageDraw.Draw(layer)
    cy = base_y - 30 * s
    for dx, rx, col in [(-42, 38, (206, 96, 22, 255)), (42, 38, (206, 96, 22, 255)),
                        (-21, 38, (226, 120, 32, 255)), (21, 38, (226, 120, 32, 255)),
                        (0, 34, (244, 146, 48, 255))]:
        d.ellipse([cx + (dx - rx) * s, cy - 46 * s, cx + (dx + rx) * s, cy + 46 * s], fill=col, outline=(150, 66, 14, 255), width=2)
    d.polygon([(cx - 7 * s, cy - 44 * s), (cx + 7 * s, cy - 44 * s), (cx + 4 * s, cy - 66 * s), (cx - 4 * s, cy - 66 * s)], fill=(96, 64, 36, 255))
    _leaf(d, cx + 26 * s, cy - 54 * s, 22 * s, math.radians(-25), (130, 160, 56, 255))


def _hero(th: dict, layer: Image.Image, cx: int, base_y: int, scale: float = 1.0):
    """Головна ілюстрація в шапці: ялинка / тюльпани / сонце / гарбуз."""
    {"winter": _tree, "spring": _tulips, "summer": _sun, "autumn": _pumpkin}[th["name"]](layer, cx, base_y, scale)


def _rounded(d, box, r, fill, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def render_schedule_card(group_name: str, data: dict, is_update: bool = False, theme: str | None = None) -> bytes:
    th = THEMES[current_theme(override=theme)]
    f_title = _font(FONT_BOLD, 58)
    f_sub = _font(FONT_BOLD, 40)
    f_day = _font(FONT_BOLD, 38)
    f_txt = _font(FONT_REG, 28)
    f_chip = _font(FONT_BOLD, 30)
    f_small = _font(FONT_REG, 22)

    layer = Image.new("RGBA", (W, 4000), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # --- шапка
    y = 110
    if is_update:
        label = "ОНОВЛЕННЯ ГРАФІКА"
        lw = d.textlength(label, font=f_small) + 40
        _rounded(d, (PAD, y, PAD + lw, y + 40), 20, (220, 50, 60, 255))
        d.text((PAD + 20, y + 6), label, font=f_small, fill=(255, 255, 255, 255))
        y += 58
    d.text((PAD, y), "Графік відключень", font=f_title, fill=(255, 255, 255, 255))
    y += 78
    d.text((PAD, y), f"Черга {group_name}", font=f_sub, fill=th["accent"])
    y += 56
    d.text((PAD, y), _greeting(th, datetime.now()), font=f_txt, fill=th["tint"])
    header_bottom = y + 50
    _hero(th, layer, W - 170, header_bottom - 40, 1.0)

    y = max(header_bottom, 340)

    # --- дні
    for day_label, info in data.get("days", {}).items():
        intervals = info.get("intervals", [])
        segs = _segments(intervals)
        chips_h = 0

        # попередній розрахунок висоти чипів
        chip_rows = 1
        if intervals:
            x = PAD + 28
            for iv in intervals:
                cw = d.textlength(iv, font=f_chip) + 36
                if x + cw > W - PAD - 28:
                    chip_rows += 1
                    x = PAD + 28
                x += cw + 14
            chips_h = chip_rows * 58
        else:
            chips_h = 50

        panel_h = 70 + 34 + 46 + chips_h + 30
        _rounded(d, (PAD, y, W - PAD, y + panel_h), 28, (255, 255, 255, 26), (255, 255, 255, 70), 2)

        d.text((PAD + 28, y + 18), day_label, font=f_day, fill=(255, 255, 255, 255))
        if intervals:
            summary = f"{len(intervals)} відкл. • {_fmt_dur(_total_minutes(intervals))}"
            sw = d.textlength(summary, font=f_txt)
            d.text((W - PAD - 28 - sw, y + 26), summary, font=f_txt, fill=(255, 190, 190, 255))
        else:
            summary = "без відключень"
            sw = d.textlength(summary, font=f_txt)
            d.text((W - PAD - 28 - sw, y + 26), summary, font=f_txt, fill=(150, 235, 170, 255))

        # 24-годинна шкала
        bx0, bx1 = PAD + 28, W - PAD - 28
        by0 = y + 80
        bw = bx1 - bx0
        _rounded(d, (bx0, by0, bx1, by0 + 34), 17, (70, 190, 110, 255))
        for s, e in segs:
            x0 = bx0 + bw * s / 1440
            x1 = bx0 + bw * e / 1440
            d.rounded_rectangle((x0, by0, x1, by0 + 34), radius=8, fill=(230, 60, 70, 255))
        for h in (0, 6, 12, 18, 24):
            tx = bx0 + bw * h / 24
            d.line([(tx, by0 + 34), (tx, by0 + 42)], fill=(200, 220, 240, 200), width=2)
            lbl = f"{h:02d}"
            lw = d.textlength(lbl, font=f_small)
            tx_text = min(max(tx - lw / 2, bx0 - 4), bx1 - lw + 4)
            d.text((tx_text, by0 + 44), lbl, font=f_small, fill=th["tint"])

        # чипи з інтервалами
        cy = by0 + 86
        if intervals:
            x = PAD + 28
            for iv in intervals:
                cw = d.textlength(iv, font=f_chip) + 36
                if x + cw > W - PAD - 28:
                    x = PAD + 28
                    cy += 58
                _rounded(d, (x, cy, x + cw, cy + 46), 23, (230, 60, 70, 255))
                d.text((x + 18, cy + 5), iv, font=f_chip, fill=(255, 255, 255, 255))
                x += cw + 14
        else:
            d.text((PAD + 28, cy + 4), "Світло є весь день", font=f_chip, fill=(150, 235, 170, 255))

        y += panel_h + 26

    # --- підвал
    stamp = datetime.now().strftime("Оновлено %d.%m.%Y о %H:%M")
    d.text((PAD, y + 6), stamp, font=f_small, fill=th["tint2"])
    total_h = y + 70

    layer = layer.crop((0, 0, W, total_h))
    img = _background(W, total_h, sum(map(ord, group_name)), th)
    img = _decor_top(img, th)
    img = Image.alpha_composite(img, layer).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# =====================================================================
#  Зведена картинка по всіх чергах (для групових чатів)
# =====================================================================
GROUPS = ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2"]
OW = 1200
OPAD = 40
OGAP = 22
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]


def _hours(minutes: int) -> str:
    return f"{minutes / 60:g}".replace(".", ",")


def _wrap_chips(d, intervals, font, avail):
    rows, cur, x = [], [], 0
    for iv in intervals:
        cw = d.textlength(iv, font=font) + 28
        if cur and x + cw > avail:
            rows.append(cur)
            cur, x = [], 0
        cur.append((iv, cw))
        x += cw + 10
    if cur:
        rows.append(cur)
    return rows


def _day_title(day_label: str) -> str:
    try:
        dt = datetime.strptime(day_label, "%d.%m.%Y")
        return f"{WEEKDAYS[dt.weekday()]}, {day_label}"
    except ValueError:
        return day_label


def render_overview_card(all_data: dict, day_label: str, changed=None, is_update: bool = False,
                         theme: str | None = None) -> bytes:
    """all_data: {"5.1": {"days": {"24.11.2026": {"intervals": [...]}}}, ...}"""
    th = THEMES[current_theme(override=theme)]
    changed = set(changed or [])
    f_title = _font(FONT_BOLD, 56)
    f_sub = _font(FONT_BOLD, 34)
    f_q = _font(FONT_BOLD, 46)
    f_chip = _font(FONT_BOLD, 26)
    f_txt = _font(FONT_BOLD, 23)
    f_small = _font(FONT_REG, 21)
    f_badge = _font(FONT_BOLD, 19)

    now = datetime.now()
    is_today = day_label == now.strftime("%d.%m.%Y")
    now_min = now.hour * 60 + now.minute

    layer = Image.new("RGBA", (OW, 5000), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # --- шапка
    y = 100
    if is_update:
        label = "ОНОВЛЕННЯ ГРАФІКА"
        lw = d.textlength(label, font=f_small) + 40
        _rounded(d, (OPAD, y, OPAD + lw, y + 40), 20, (220, 50, 60, 255))
        d.text((OPAD + 20, y + 6), label, font=f_small, fill=(255, 255, 255, 255))
        y += 56
    d.text((OPAD, y), "Графік відключень", font=f_title, fill=(255, 255, 255, 255))
    y += 76
    d.text((OPAD, y), f"Львівська область  •  {_day_title(day_label)}", font=f_sub, fill=th["accent"])
    y += 58
    _hero(th, layer, OW - 130, 270, 0.75)
    grid_y = max(y + 14, 300)

    # --- підготовка даних клітинок
    cell_w = (OW - 2 * OPAD - OGAP) // 2
    cells = []
    for g in GROUPS:
        info = (all_data.get(g) or {}).get("days", {}).get(day_label)
        intervals = info.get("intervals", []) if info else None
        rows = _wrap_chips(d, intervals, f_chip, cell_w - 40) if intervals else []
        cells.append((g, intervals, rows))

    def cell_height(rows):
        return 16 + 58 + 18 + 16 + max(1, len(rows)) * 46 + 10 + 34 + 12

    # --- сітка 2 x 6
    y = grid_y
    for r in range(0, len(cells), 2):
        pair = cells[r:r + 2]
        row_h = max(cell_height(c[2]) for c in pair)
        for col, (g, intervals, rows) in enumerate(pair):
            x = OPAD + col * (cell_w + OGAP)
            is_changed = g in changed
            _rounded(
                d, (x, y, x + cell_w, y + row_h), 26, (255, 255, 255, 26),
                th["accent"] if is_changed else (255, 255, 255, 70), 4 if is_changed else 2,
            )
            d.text((x + 22, y + 10), g, font=f_q, fill=th["accent"])

            if is_changed:
                lbl = "ОНОВЛЕНО"
                lw = d.textlength(lbl, font=f_badge) + 24
                _rounded(d, (x + cell_w - 20 - lw, y + 20, x + cell_w - 20, y + 50), 15, th["accent"])
                d.text((x + cell_w - 8 - lw, y + 25), lbl, font=f_badge, fill=(40, 30, 0, 255))

            # шкала доби
            bx0, bx1 = x + 22, x + cell_w - 22
            by = y + 16 + 58 + 4
            bw = bx1 - bx0
            segs = _segments(intervals or [])
            if intervals is None:
                _rounded(d, (bx0, by, bx1, by + 18), 9, (120, 140, 170, 160))
            else:
                _rounded(d, (bx0, by, bx1, by + 18), 9, (70, 190, 110, 255))
                for s, e in segs:
                    d.rounded_rectangle((bx0 + bw * s / 1440, by, bx0 + bw * e / 1440, by + 18), radius=5, fill=(230, 60, 70, 255))
            for h in (6, 12, 18):
                tx = bx0 + bw * h / 24
                d.line([(tx, by), (tx, by + 18)], fill=th["top"] + (150,), width=2)
            if is_today:
                nx = bx0 + bw * now_min / 1440
                d.line([(nx, by - 5), (nx, by + 23)], fill=(255, 255, 255, 255), width=3)
                d.ellipse([nx - 5, by - 11, nx + 5, by - 1], fill=(255, 255, 255, 255))

            # інтервали
            cy = by + 18 + 16
            if intervals is None:
                d.text((x + 22, cy + 6), "немає даних", font=f_chip, fill=(170, 190, 215, 255))
            elif not intervals:
                d.text((x + 22, cy + 6), "Відключень немає", font=f_chip, fill=(150, 235, 170, 255))
            else:
                for ri, row in enumerate(rows):
                    cx = x + 22
                    for iv, cw in row:
                        _rounded(d, (cx, cy + ri * 46, cx + cw, cy + ri * 46 + 38), 19, (230, 60, 70, 255))
                        d.text((cx + 14, cy + ri * 46 + 4), iv, font=f_chip, fill=(255, 255, 255, 255))
                        cx += cw + 10

            # низ клітинки: підсумок
            fy = y + row_h - 12 - 30
            off_min = sum(e - s for s, e in segs)
            if intervals is not None:
                right = f"без світла {_hours(off_min)} год"
                rw = d.textlength(right, font=f_txt)
                d.text((x + cell_w - 22 - rw, fy), right, font=f_txt, fill=(255, 175, 175, 255))
                d.text((x + 22, fy), f"світло {_hours(1440 - off_min)} год", font=f_txt, fill=(150, 235, 170, 255))
                if is_today:
                    on_now = not any(s <= now_min < e for s, e in segs)
                    dot = (90, 220, 120, 255) if on_now else (255, 82, 82, 255)
                    qx = x + 22 + d.textlength(g, font=f_q) + 18
                    d.ellipse([qx, y + 26, qx + 18, y + 44], fill=dot, outline=(255, 255, 255, 230), width=2)
        y += row_h + OGAP

    # --- підвал: легенда
    ly = y + 4
    lx = OPAD
    for color, text in (((70, 190, 110, 255), "є світло"), ((230, 60, 70, 255), "немає світла")):
        _rounded(d, (lx, ly + 4, lx + 34, ly + 22), 9, color)
        d.text((lx + 44, ly), text, font=f_small, fill=th["tint3"])
        lx += 44 + d.textlength(text, font=f_small) + 34
    if is_today:
        d.line([(lx + 8, ly), (lx + 8, ly + 26)], fill=(255, 255, 255, 255), width=3)
        d.text((lx + 24, ly), "зараз  (крапка біля черги: зелена — світло є, червона — немає)", font=f_small, fill=th["tint3"])
    stamp = now.strftime("Оновлено %d.%m.%Y о %H:%M  •  poweron.loe.lviv.ua")
    d.text((OPAD, ly + 40), stamp, font=f_small, fill=th["tint2"])
    total_h = ly + 100

    layer = layer.crop((0, 0, OW, total_h))
    img = _background(OW, total_h, sum(map(ord, day_label)), th)
    img = _decor_top(img, th)
    img = Image.alpha_composite(img, layer).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


if __name__ == "__main__":
    # python card.py            — демо поточної теми; python card.py all — демо всіх чотирьох сезонів
    import sys

    demo = {
        "days": {
            "24.11.2026": {"intervals": ["02:00–06:00", "10:30–14:00", "18:00–22:30"]},
            "25.11.2026": {"intervals": []},
        }
    }
    day = datetime.now().strftime("%d.%m.%Y")
    sample = {
        "1.1": ["08:00–12:30"], "1.2": ["19:30–22:00"], "2.1": ["22:00–00:00"], "2.2": [],
        "3.1": [], "3.2": ["08:00–12:30"], "4.1": [], "4.2": ["01:00–03:30", "09:00–11:30", "15:00–17:30", "21:00–23:30"],
        "5.1": ["16:00–20:00"], "5.2": ["12:30–16:00"], "6.1": ["12:30–16:00"], "6.2": ["16:00–19:30"],
    }
    all_demo = {g: {"days": {day: {"intervals": iv}}} for g, iv in sample.items()}
    themes = list(THEMES) if "all" in sys.argv[1:] else [None]
    for t in themes:
        suffix = f"_{t}" if t else ""
        with open(f"card_demo{suffix}.png", "wb") as f:
            f.write(render_schedule_card("5.1", demo, is_update=True, theme=t))
        with open(f"overview_demo{suffix}.png", "wb") as f:
            f.write(render_overview_card(all_demo, day, changed={"5.1", "6.2"}, is_update=True, theme=t))
    print("Збережено демо-картинки:", ", ".join(THEMES) if len(themes) > 1 else current_theme())

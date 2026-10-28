# -*- coding: utf-8 -*-
"""把结构化内容渲染成「视频总结一图读懂」PNG + 写出 md 文档。

用法:
  python render_summary.py --plan-file plan.json

plan.json 结构（缺失字段会用默认值兜底）:
{
  "outDir": "输出目录（可选）",
  "md": "markdown 全文（可选，给了就写文件）",
  "mdFileName": "解析-xxx.md",
  "pngFileName": "视频总结一图读懂.png",
  "copyTranscripts": ["transcript.txt 的路径", ...],
  "plan": {
    "title": "一张图读懂《...》",
    "subtitle": "UP 主 · 时长 · 播放 · 日期",
    "quoteLabel": "一句话主旨",
    "quote": "...",
    "pitfallsTitle": "他年轻时踩过的三个坑",
    "pitfalls": ["...", "..."],
    "goalTitle": "目标：...",
    "goalBasis": "按 ... 的算例",
    "milestones": [{"amount":"20 万","tag":"灯塔 1 · 第 8 年","desc":"...","color":"blue","connector":"再 7 年"}],
    "milestoneNote": "...",
    "suggestionsTitle": "...",
    "suggestionsHint": "...",
    "suggestions": [{"title":"...","bullets":["..."]}],
    "caveatsTitle": "打个折扣",
    "caveats": ["..."],
    "todosTitle": "可直接抄的 4 件事",
    "todos": ["..."],
    "footer": "来源说明"
  }
}

末尾输出一行 RESULT_JSON:{...}。
"""
import argparse
import json
import os
import re
import shutil
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1320
BG = (247, 248, 251)
TEXT = (31, 36, 48)
GRAY = (92, 100, 114)
BLUE = (47, 107, 255)
BLUE_DARK = (30, 64, 175)
BLUE_BG = (234, 241, 255)
BLUE_LINE = (201, 217, 255)
ORANGE = (232, 137, 12)
ORANGE_DARK = (160, 84, 8)
ORANGE_BG = (255, 249, 242)
ORANGE_LINE = (240, 178, 92)
RED = (163, 62, 40)
RED_BG = (255, 247, 245)
RED_LINE = (234, 192, 182)
GREEN = (31, 122, 77)
GREEN_BG = (241, 250, 244)
GREEN_LINE = (159, 203, 176)
PURPLE = (109, 76, 196)
PURPLE_BG = (245, 242, 255)
PURPLE_LINE = (196, 184, 235)
WHITE = (255, 255, 255)

FD = r"C:\Windows\Fonts"


def font_file(name, fallback):
    path = os.path.join(FD, name)
    return path if os.path.exists(path) else fallback


REG = font_file("msyh.ttc", "simhei.ttf")
BOLD = font_file("msyhbd.ttc", font_file("simhei.ttf", REG))


def F(path, size):
    return ImageFont.truetype(path, size)


F_TITLE = F(BOLD, 52)
F_SUB = F(REG, 22)
F_QUOTE_LABEL = F(BOLD, 20)
F_QUOTE = F(BOLD, 27)
F_SEC = F(BOLD, 29)
F_PANEL_TITLE = F(BOLD, 26)
F_ITEM = F(REG, 20)
F_CARD_TITLE = F(BOLD, 20)
F_CARD = F(REG, 16.5)
F_CARD_SMALL = F(REG, 15.5)
F_BIG = F(BOLD, 40)
F_TAG = F(REG, 17)
F_NOTE = F(BOLD, 19)
F_FOOT = F(REG, 17)
F_SEC_SMALL = F(BOLD, 23)


def wrap(text, fnt, max_w):
    if "\n" in text:
        out = []
        for seg in text.split("\n"):
            out.extend(wrap(seg, fnt, max_w))
        return out
    tokens, i = [], 0
    while i < len(text):
        ch = text[i]
        if ord(ch) < 128 and ch not in " ，。、；：（）《》「」":
            j = i
            while j < len(text) and ord(text[j]) < 128 and text[j] not in " ，。、；：（）《》「」":
                j += 1
            tokens.append(text[i:j])
            i = j
        else:
            tokens.append(ch)
            i += 1
    lines, cur = [], ""
    for tok in tokens:
        if fnt.getlength(cur + tok) <= max_w or not cur:
            cur += tok
        else:
            lines.append(cur)
            cur = tok
    if cur:
        lines.append(cur)
    punct = "，。、；：！？）》」』"
    for k in range(1, len(lines)):
        while lines[k] and lines[k][0] in punct:
            lines[k - 1] += lines[k][0]
            lines[k] = lines[k][1:]
    return [ln for ln in lines if ln]


def para(d, x, y, max_w, text, fnt, fill, lh, max_lines=99):
    lines = wrap(text, fnt, max_w)
    for k, ln in enumerate(lines[:max_lines]):
        d.text((x, y + k * lh), ln, font=fnt, fill=fill)
    return y + min(len(lines), max_lines) * lh


def badge(d, cx, cy, r, fill, label, fnt):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    d.text((cx, cy), label, font=fnt, fill=WHITE, anchor="mm")


def check(d, x, y, color):
    d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=color)
    d.line((x - 6, y, x - 1, y + 6), fill=WHITE, width=3)
    d.line((x - 1, y + 6, x + 7, y - 5), fill=WHITE, width=3)


COLOR_MAP = {
    "blue": (BLUE, BLUE, BLUE_LINE),
    "orange": (ORANGE, ORANGE, ORANGE_LINE),
    "green": (GREEN, GREEN, GREEN_LINE),
    "purple": (PURPLE, PURPLE, PURPLE_LINE),
}


def render(plan):
    p = plan.get("plan") or {}
    if p.get("cards"):
        return render_cards(p)
    pitfalls = [str(x) for x in (p.get("pitfalls") or [])][:5]
    milestones = [m for m in (p.get("milestones") or [])][:4]
    suggestions = [s for s in (p.get("suggestions") or [])][:5]
    caveats = [str(x) for x in (p.get("caveats") or [])][:5]
    todos = [str(x) for x in (p.get("todos") or [])][:5]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # header
    title = str(p.get("title") or "一张图读懂这个视频")
    d.text((44, 28), title, font=F_TITLE, fill=TEXT)
    d.text((46, 104), str(p.get("subtitle") or ""), font=F_SUB, fill=GRAY)

    # quote
    d.rounded_rectangle((44, 158, 1876, 262), radius=16, fill=BLUE_BG, outline=BLUE_LINE, width=2)
    d.text((68, 174), str(p.get("quoteLabel") or "一句话主旨"), font=F_QUOTE_LABEL, fill=BLUE_DARK)
    para(d, 68, 204, 1740, str(p.get("quote") or ""), F_QUOTE, TEXT, 34, max_lines=1)

    PY, PH = 294, 356

    # pitfalls panel
    d.rounded_rectangle((44, PY, 604, PY + PH), radius=16, fill=RED_BG, outline=RED_LINE, width=2)
    d.text((68, PY + 22), str(p.get("pitfallsTitle") or "他年轻时踩过的坑"), font=F_PANEL_TITLE, fill=RED)
    if pitfalls:
        step = min(104, int((PH - 84) / len(pitfalls)))
        y = PY + 76
        for i, it in enumerate(pitfalls):
            badge(d, 90, y + 16, 17, RED, str(i + 1), F(BOLD, 19))
            para(d, 118, y, 470, it, F_ITEM, TEXT, 30, max_lines=2)
            y += step

    # goal + milestones
    d.rounded_rectangle((644, PY, 1876, PY + PH), radius=16, fill=WHITE, outline=BLUE_LINE, width=2)
    d.text((668, PY + 22), str(p.get("goalTitle") or "目标"), font=F_PANEL_TITLE, fill=BLUE_DARK)
    d.text((668, PY + 62), str(p.get("goalBasis") or ""), font=F_TAG, fill=GRAY)

    if milestones:
        inner_x, inner_w = 668, 1184
        n = len(milestones)
        gap = 74 if n <= 3 else 40
        cw = min(400, int((inner_w - (n - 1) * gap) / n))
        ch = 206
        total_w = n * cw + (n - 1) * gap
        start_x = inner_x + max(0, (inner_w - total_w) // 2)
        yy = PY + 108
        for i, m in enumerate(milestones):
            color, color_dark, _ = COLOR_MAP.get(m.get("color") or "blue", COLOR_MAP["blue"])
            x = start_x + i * (cw + gap)
            d.rounded_rectangle((x, yy, x + cw, yy + ch), radius=14, fill=WHITE, outline=color, width=3)
            d.text((x + cw / 2, yy + 18), str(m.get("amount") or ""), font=F_BIG, fill=color, anchor="ma")
            d.text((x + cw / 2, yy + 74), str(m.get("tag") or ""), font=F_TAG, fill=GRAY, anchor="ma")
            d.text((x + cw / 2, yy + 108), str(m.get("desc") or ""), font=F(BOLD, 18), fill=TEXT, anchor="ma")
            if i < n - 1:
                ax1, ax2 = x + cw + 6, x + cw + gap - 8
                ay = yy + 86
                d.line((ax1, ay, ax2 - 10, ay), fill=(160, 170, 188), width=3)
                d.polygon([(ax2, ay), (ax2 - 14, ay - 7), (ax2 - 14, ay + 7)], fill=(160, 170, 188))
                # connector 表示「到达该里程碑所需时间」，用于它前面那段箭头
                connector = str(milestones[i + 1].get("connector") or "")
                if connector:
                    d.text(((ax1 + ax2) / 2, ay - 30), connector, font=F_TAG, fill=GRAY, anchor="ma")
    note = str(p.get("milestoneNote") or "")
    if note:
        para(d, 668, PY + 324, 1180, note, F_NOTE, ORANGE_DARK, 24, max_lines=1)

    # suggestions
    if suggestions:
        d.text((44, 684), str(p.get("suggestionsTitle") or "建议"), font=F_SEC, fill=TEXT)
        hint = str(p.get("suggestionsHint") or "")
        if hint:
            d.text((1876 - d.textlength(hint, font=F_TAG) - 44, 700), hint, font=F_TAG, fill=GRAY)
        CY, CH = 734, 300
        n = len(suggestions)
        gap = 20
        sw = int((1832 - (n - 1) * gap) / n)
        sw = min(sw, 520)
        total_w = n * sw + (n - 1) * gap
        start_x = 44 + max(0, (1832 - total_w) // 2)
        for i, s in enumerate(suggestions):
            x = start_x + i * (sw + gap)
            d.rounded_rectangle((x, CY, x + sw, CY + CH), radius=14, fill=WHITE, outline=BLUE_LINE, width=2)
            badge(d, x + 40, CY + 40, 18, BLUE, str(i + 1), F(BOLD, 19))
            lines = wrap(str(s.get("title") or ""), F_CARD_TITLE, sw - 80)
            for k, ln in enumerate(lines[:2]):
                d.text((x + 66, CY + 26 + k * 27), ln, font=F_CARD_TITLE, fill=TEXT)
            by = CY + 78 + (min(len(lines), 2) - 1) * 27 + 22
            for b in (s.get("bullets") or [])[:5]:
                d.ellipse((x + 22, by + 8, x + 29, by + 15), fill=BLUE)
                by = para(d, x + 38, by, sw - 58, str(b), F_CARD, (60, 68, 84), 23, max_lines=3) + 15

    # caveats + todos
    DY, DH = 1064, 192
    left_x, right_x, panel_w = 44, 976, 900
    d.rounded_rectangle((left_x, DY, left_x + panel_w, DY + DH), radius=16,
                        fill=ORANGE_BG, outline=ORANGE_LINE, width=2)
    d.text((left_x + 24, DY + 18), str(p.get("caveatsTitle") or "打个折扣"), font=F_SEC_SMALL, fill=ORANGE_DARK)
    if caveats:
        y = DY + 56
        for c in caveats:
            d.ellipse((left_x + 26, y + 8, left_x + 33, y + 15), fill=ORANGE)
            y = para(d, left_x + 44, y, panel_w - 70, c, F_CARD_SMALL, (74, 82, 97), 22, max_lines=2) + 6

    d.rounded_rectangle((right_x, DY, right_x + panel_w, DY + DH), radius=16,
                        fill=GREEN_BG, outline=GREEN_LINE, width=2)
    d.text((right_x + 24, DY + 18), str(p.get("todosTitle") or "可以直接做的事"), font=F_SEC_SMALL, fill=GREEN)
    if todos:
        y = DY + 58
        for t in todos:
            check(d, right_x + 36, y + 11, GREEN)
            y = para(d, right_x + 58, y, panel_w - 84, t, F_CARD_SMALL, TEXT, 22, max_lines=2) + 6

    footer = str(p.get("footer") or "来源：视频音轨本地 ASR 转录后人工整理；个别数字与专有名词可能与原视频有出入，引用前建议复核。")
    d.text((44, H - 44), footer, font=F_FOOT, fill=GRAY)
    return img


# ---------------- 卡片版式（榜单 / 插件 / 清单类视频） ----------------
CW, CH = 1920, 1390
DENG = font_file("Deng.ttf", REG)
DENGB = font_file("Dengb.ttf", BOLD)
MONO = font_file("consolab.ttf", BOLD)
MONO_REG = font_file("consola.ttf", REG)


def _is_ascii(s):
    return all(ord(ch) < 128 for ch in s)


def _mix(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


ACCENTS = {
    "blue": ((47, 107, 255), (30, 64, 175), (241, 245, 255)),
    "amber": ((226, 132, 10), (146, 80, 8), (255, 247, 233)),
    "rose": ((210, 62, 116), (146, 36, 80), (253, 240, 246)),
    "violet": ((122, 90, 214), (80, 54, 156), (245, 241, 255)),
    "green": ((28, 134, 82), (18, 96, 58), (236, 249, 242)),
    "red": ((201, 66, 58), (146, 42, 36), (254, 240, 238)),
}


def _vert_grad(w, h, top, bottom):
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def _soft_shadow(size, boxes, radius=20, blur=13, alpha=34, dy=9):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    dl = ImageDraw.Draw(layer)
    for b in boxes:
        dl.rounded_rectangle((b[0], b[1] + dy, b[2], b[3] + dy), radius=radius, fill=(23, 32, 56, alpha))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def _title_two_tone(d, x, y, text, hl, fnt, color, hl_color):
    if not hl or hl not in text:
        d.text((x, y), text, font=fnt, fill=color)
        return
    pre, post = text.split(hl, 1)
    d.text((x, y), pre, font=fnt, fill=color)
    x2 = x + fnt.getlength(pre)
    d.text((x2, y), hl, font=fnt, fill=hl_color)
    d.text((x2 + fnt.getlength(hl), y), post, font=fnt, fill=color)


def render_cards(p):
    cards = [c for c in (p.get("cards") or [])][:12]
    panels = (p.get("panels") or [])[:3]

    f_kick = F(DENG, 17)
    f_title = F(DENGB, 58)
    f_sub = F(DENG, 21)
    f_q_label = F(DENGB, 16)
    f_quote = F(DENGB, 26)
    f_en = F(MONO, 25)
    f_en_s = F(MONO, 20)
    f_cn = F(DENGB, 26)
    f_num = F(MONO, 21)
    f_zh = F(DENGB, 19)
    f_desc = F(DENG, 16.5)
    f_bul = F(DENG, 16)
    f_sec = F(DENGB, 21)
    f_item = F(DENG, 16)
    f_foot = F(DENG, 14.5)
    f_stat_n = F(DENGB, 30)
    f_stat_l = F(DENG, 14.5)

    # 先量后画：卡片高度按内容自适应，整张图的高度跟着内容走
    gx, gy, cw, gpx, gpy = 56, 322, 434, 24, 24
    need = 300
    for c in cards:
        h = 130 + 8 + len(wrap(str(c.get("desc") or ""), f_desc, cw - 44)[:3]) * 26 + 8
        for b in (c.get("bullets") or [])[:3]:
            h += len(wrap(str(b), f_bul, cw - 66)[:2]) * 25 + 6
        need = max(need, h + 30)
    chh = max(300, min(404, need))
    rows = max(1, (len(cards) + 3) // 4)
    grid_bottom = gy + rows * chh + (rows - 1) * gpy
    ph = 200
    py0 = grid_bottom + 34
    W2 = CW
    H2 = py0 + ph + 78 if panels else grid_bottom + 80

    img = _vert_grad(W2, H2, (253, 253, 254), (236, 240, 247)).convert("RGBA")
    d = ImageDraw.Draw(img)

    # 眉标 + 标题 + 副标题
    d.text((56, 40), str(p.get("kicker") or "B 站视频一图读懂 · 本地 ASR + 抽帧校对"),
           font=f_kick, fill=(138, 147, 165))
    _title_two_tone(d, 54, 66, str(p.get("title") or "一张图读懂这个视频"),
                    str(p.get("titleHl") or ""), f_title, (16, 24, 40), (47, 107, 255))
    d.text((58, 150), str(p.get("subtitle") or ""), font=f_sub, fill=(102, 112, 133))

    # 右上角数据
    sx = 1864
    for st in reversed((p.get("stats") or [])[:3]):
        value, label = str(st.get("value") or ""), str(st.get("label") or "")
        bw = max(f_stat_n.getlength(value), f_stat_l.getlength(label)) + 6
        cx = sx - bw / 2
        d.text((cx, 58), value, font=f_stat_n, fill=(16, 24, 40), anchor="mt")
        d.text((cx, 104), label, font=f_stat_l, fill=(142, 150, 166), anchor="mt")
        sx -= bw + 44

    # 主旨条
    d.rounded_rectangle((56, 200, 1864, 292), radius=18, fill=(242, 246, 255), outline=(216, 227, 255), width=1)
    d.rounded_rectangle((56, 200, 63, 292), radius=3, fill=(47, 107, 255))
    d.text((86, 216), str(p.get("quoteLabel") or "一句话主旨"), font=f_q_label, fill=(59, 91, 219))
    para(d, 86, 240, 1730, str(p.get("quote") or ""), f_quote, (24, 32, 48), 34, max_lines=1)

    # 一条淡色渐变：把八个插件串成一根线
    nbar = min(len(cards), 8)
    if nbar:
        seg = 1808 // nbar
        for i in range(nbar):
            accent = ACCENTS.get(str(cards[i].get("color") or "blue"), ACCENTS["blue"])[0]
            sx0 = 56 + i * seg
            d.rounded_rectangle((sx0 + 1, 302, sx0 + seg - 3, 309), radius=3,
                                fill=_mix(accent, (255, 255, 255), 0.45))

    # 卡片网格
    boxes = []
    for i in range(len(cards)):
        col, row = i % 4, i // 4
        x = gx + col * (cw + gpx)
        y = gy + row * (chh + gpy)
        boxes.append((x, y, x + cw, y + chh))
    if boxes:
        img = Image.alpha_composite(img, _soft_shadow(img.size, boxes))
        d = ImageDraw.Draw(img)

    for i, c in enumerate(cards):
        x, y = boxes[i][0], boxes[i][1]
        accent, accent_dark, _tint = ACCENTS.get(str(c.get("color") or "blue"), ACCENTS["blue"])
        d.rounded_rectangle((x, y, x + cw, y + chh), radius=20, fill=(255, 255, 255),
                            outline=(229, 233, 241), width=1)
        d.rounded_rectangle((x + 22, y + 15, x + 68, y + 19), radius=2, fill=accent)
        d.rounded_rectangle((x + 22, y + 30, x + 84, y + 68), radius=11, fill=accent)
        d.text((x + 53, y + 49), str(c.get("num") or "%02d" % (i + 1)), font=f_num,
               fill=(255, 255, 255), anchor="mm")
        en = str(c.get("en") or "")
        fnt_en = f_en if _is_ascii(en) else f_cn
        if fnt_en.getlength(en) > cw - 110:
            fnt_en = f_en_s if _is_ascii(en) else F(DENGB, 22)
        d.text((x + 96, y + 49), en, font=fnt_en, fill=(16, 24, 40), anchor="lm")
        d.text((x + 22, y + 80), str(c.get("zh") or ""), font=f_zh, fill=accent_dark)
        d.line((x + 22, y + 116, x + cw - 22, y + 116), fill=(237, 240, 246), width=1)

        ty = y + 130
        for ln in wrap(str(c.get("desc") or ""), f_desc, cw - 44)[:3]:
            d.text((x + 22, ty), ln, font=f_desc, fill=(70, 80, 100))
            ty += 26
        by = ty + 8
        for b in (c.get("bullets") or [])[:3]:
            if by > y + chh - 40:
                break
            d.ellipse((x + 22, by + 8, x + 30, by + 16), fill=accent)
            by = para(d, x + 40, by, cw - 66, str(b), f_bul, (44, 52, 70), 25, max_lines=2) + 6

    # 底部三块
    if panels:
        n = len(panels)
        gap = 24
        pw = int((1808 - (n - 1) * gap) / n)
        pboxes = [(56 + i * (pw + gap), py0, 56 + i * (pw + gap) + pw, py0 + ph) for i in range(n)]
        img = Image.alpha_composite(img, _soft_shadow(img.size, pboxes, radius=18, blur=11, alpha=24, dy=7))
        d = ImageDraw.Draw(img)
        for i, pn in enumerate(panels):
            accent, accent_dark, tint = ACCENTS.get(str(pn.get("color") or "blue"), ACCENTS["blue"])
            x0, y0 = pboxes[i][0], pboxes[i][1]
            d.rounded_rectangle((x0, y0, x0 + pw, y0 + ph), radius=18, fill=tint,
                                outline=(255, 255, 255), width=1)
            d.text((x0 + 24, y0 + 16), str(pn.get("title") or ""), font=f_sec, fill=accent_dark)
            ty = y0 + 54
            ordered = bool(pn.get("ordered"))
            for k, it in enumerate((pn.get("items") or [])[:4]):
                if ty > y0 + ph - 32:
                    break
                if ordered:
                    d.ellipse((x0 + 26, ty + 2, x0 + 46, ty + 22), fill=accent)
                    d.text((x0 + 36, ty + 12), str(k + 1), font=F(MONO_REG, 12.5),
                           fill=(255, 255, 255), anchor="mm")
                    ty = para(d, x0 + 56, ty, pw - 82, str(it), f_item, (46, 54, 72), 24, max_lines=2) + 7
                else:
                    d.ellipse((x0 + 27, ty + 8, x0 + 35, ty + 16), fill=accent)
                    ty = para(d, x0 + 44, ty, pw - 70, str(it), f_item, (46, 54, 72), 24, max_lines=2) + 7

    d.text((56, H2 - 34), str(p.get("footer") or ""), font=f_foot, fill=(138, 147, 165))
    return img.convert("RGB")


def safe_name(name, default):
    name = (name or default or "文件").strip()
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "-", name)
    return name[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-file", required=True)
    args = ap.parse_args()
    try:
        with open(args.plan_file, encoding="utf-8") as f:
            payload = json.load(f)
        out_dir = payload.get("outDir") or os.path.join(os.path.expanduser("~"), ".dsh",
                                                         "bili-video-summary", "out", "video")
        os.makedirs(out_dir, exist_ok=True)
        img = render(payload)
        png_name = safe_name(payload.get("pngFileName"), "视频总结一图读懂.png")
        if not png_name.lower().endswith(".png"):
            png_name += ".png"
        png_path = os.path.join(out_dir, png_name)
        img.save(png_path, "PNG")

        md_path = ""
        md = payload.get("md")
        if md:
            md_name = safe_name(payload.get("mdFileName"), "解析-视频.md")
            if not md_name.lower().endswith(".md"):
                md_name += ".md"
            md_path = os.path.join(out_dir, md_name)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md)

        copied = []
        for src in payload.get("copyTranscripts") or []:
            if src and os.path.exists(src):
                dst = os.path.join(out_dir, os.path.basename(src))
                shutil.copy2(src, dst)
                copied.append(dst)

        print("RESULT_JSON:" + json.dumps({
            "ok": True,
            "outDir": out_dir,
            "pngPath": png_path,
            "pngBytes": os.path.getsize(png_path),
            "mdPath": md_path,
            "mdChars": len(md or ""),
            "copied": copied,
        }, ensure_ascii=False), flush=True)
    except Exception as e:  # noqa: BLE001
        print("RESULT_JSON:" + json.dumps({"ok": False, "error": str(e)}, ensure_ascii=True), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

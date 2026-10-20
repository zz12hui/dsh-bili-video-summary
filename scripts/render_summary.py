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

from PIL import Image, ImageDraw, ImageFont

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

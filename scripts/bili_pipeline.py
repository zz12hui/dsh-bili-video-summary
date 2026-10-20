# -*- coding: utf-8 -*-
"""B 站视频本地转写流水线（DSH 插件 dsh-local-bili-summary 的后端）。

用法:
  python bili_pipeline.py transcribe --url <b站链接或BV号> [--force] [--clip 秒数]

行为:
  1. 解析 BV 号，调 B 站公开 API 取元数据（免登录）
  2. 取 DASH 音轨（30280 优先），带 UA/Referer 下载到缓存目录
  3. 用 PyAV 解码为 16kHz 单声道 float32（绕过 faster-whisper 与 av>=19 的兼容问题）
  4. faster-whisper(int8) 本地转写，输出 txt / srt / tsv
  5. 末尾向 stdout 打印一行 RESULT_JSON:{...} 供 Node 端解析

环境变量:
  BILI_SUMMARY_HOME   数据根目录（默认 ~/.dsh/bili-video-summary）
  BILI_SUMMARY_PYLIBS python 依赖目录（默认 $HOME/pylibs）
  BILI_SUMMARY_MODEL  模型目录（默认 $HOME/model/faster-whisper-small）
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

HOME = os.environ.get("BILI_SUMMARY_HOME") or os.path.join(os.path.expanduser("~"), ".dsh", "bili-video-summary")
PYLIBS = os.environ.get("BILI_SUMMARY_PYLIBS") or os.path.join(HOME, "pylibs")
MODEL_DIR = os.environ.get("BILI_SUMMARY_MODEL") or os.path.join(HOME, "model", "faster-whisper-small")
CACHE = os.path.join(HOME, "cache")
WORK = os.path.join(HOME, "work")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
API_VIEW = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
API_PLAY = ("https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}"
            "&fnval=16&fnver=0&fourk=0")


def log(msg):
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def emit(payload):
    try:
        print("RESULT_JSON:" + json.dumps(payload, ensure_ascii=False), flush=True)
    except Exception:  # noqa: BLE001
        print("RESULT_JSON:" + json.dumps({"ok": False, "error": "序列化结果失败"}, ensure_ascii=True), flush=True)


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_bvid(text):
    m = re.search(r"(BV[0-9A-Za-z]{10})", text or "")
    if not m:
        raise ValueError("链接里没有找到 BV 号，例如 https://www.bilibili.com/video/BV1xx411c7mD/")
    return m.group(1)


def fmt_duration(sec):
    sec = int(sec or 0)
    return "%d:%02d" % (sec // 60, sec % 60)


def ensure_dirs():
    for d in (HOME, PYLIBS, CACHE, WORK):
        os.makedirs(d, exist_ok=True)


# ---------------- 元数据 ----------------
def fetch_meta(bvid):
    data = http_json(API_VIEW.format(bvid=bvid))
    if data.get("code") != 0:
        raise RuntimeError("B 站接口返回错误：%s %s" % (data.get("code"), data.get("message")))
    v = data["data"]
    return {
        "bvid": v["bvid"],
        "aid": v["aid"],
        "cid": v["cid"],
        "title": v["title"],
        "owner": v["owner"]["name"],
        "ownerMid": v["owner"]["mid"],
        "durationSec": v["duration"],
        "durationText": fmt_duration(v["duration"]),
        "pubdate": time.strftime("%Y-%m-%d %H:%M", time.localtime(v["pubdate"])),
        "desc": v.get("desc") or "",
        "tags": [],
        "stats": {
            "view": v.get("stat", {}).get("view", 0),
            "like": v.get("stat", {}).get("like", 0),
            "coin": v.get("stat", {}).get("coin", 0),
        },
        "pages": len(v.get("pages") or [1]),
    }


def fetch_tags(bvid):
    try:
        data = http_json("https://api.bilibili.com/x/web-interface/view/detail?bvid=" + bvid)
        return [t.get("tag_name") for t in (data.get("data", {}).get("Tags") or []) if t.get("tag_name")]
    except Exception:  # noqa: BLE001
        return []


# ---------------- 音频 ----------------
def download_audio(meta, audio_path):
    data = http_json(API_PLAY.format(bvid=meta["bvid"], cid=meta["cid"]))
    if data.get("code") != 0:
        raise RuntimeError("取播放地址失败：%s %s" % (data.get("code"), data.get("message")))
    play = data["data"]
    urls = []
    dash = play.get("dash") or {}
    if dash.get("audio"):
        best = max(dash["audio"], key=lambda a: a.get("bandwidth") or 0)
        urls.append(best["baseUrl"])
        urls.extend(best.get("backupUrl") or [])
        quality = "%d kbps" % int((best.get("bandwidth") or 0) / 1000)
    else:
        # 极少数视频只有 durl：取 mp4，av 也能解出音轨
        for item in play.get("durl") or []:
            urls.append(item["url"])
            urls.extend(item.get("backup_url") or [])
        quality = "durl mp4"
    if not urls:
        raise RuntimeError("这个视频没有可下载的音频流")

    last_err = None
    for url in urls:
        try:
            _download(url, audio_path)
            size = os.path.getsize(audio_path)
            log("音频下载完成：%.1f MB（%s）" % (size / 1048576.0, quality))
            return {"audioPath": audio_path, "audioMB": round(size / 1048576.0, 2), "quality": quality}
        except Exception as e:  # noqa: BLE001
            last_err = e
            log("下载失败，换备用地址：%s" % e)
    raise RuntimeError("音频下载失败：%s" % last_err)


def _download(url, out_path):
    req = urllib.request.Request(url, headers=HEADERS)
    part = out_path + ".part"
    with urllib.request.urlopen(req, timeout=90) as resp, open(part, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -10
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = int(done * 100 / total)
                if pct >= last_pct + 20:
                    last_pct = pct
                    log("下载音频 %d%%（%.1f/%.1f MB）" % (pct, done / 1048576.0, total / 1048576.0))
    os.replace(part, out_path)


# ---------------- 解码 ----------------
def ensure_pylibs():
    if PYLIBS not in sys.path:
        sys.path.insert(0, PYLIBS)


def decode_audio(audio_path, clip_seconds=0):
    ensure_pylibs()
    import av
    import numpy as np

    t0 = time.time()
    container = av.open(audio_path, mode="r")
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    pieces = []
    for frame in container.decode(stream):
        out = resampler.resample(frame)
        if not isinstance(out, (list, tuple)):
            out = [out]
        pieces.extend(f.to_ndarray().reshape(-1) for f in out)
    flush = resampler.resample(None)
    if flush:
        if not isinstance(flush, (list, tuple)):
            flush = [flush]
        pieces.extend(f.to_ndarray().reshape(-1) for f in flush)
    container.close()
    audio = np.concatenate(pieces).astype(np.float32) if pieces else np.zeros(16000, dtype=np.float32)
    if clip_seconds:
        audio = audio[: int(clip_seconds) * 16000]
    log("解码完成：%.1f 秒音频，耗时 %.1fs" % (len(audio) / 16000.0, time.time() - t0))
    return audio


# ---------------- 转写 ----------------
def transcribe(audio, meta):
    ensure_pylibs()
    from faster_whisper import WhisperModel

    if not os.path.isdir(MODEL_DIR):
        raise RuntimeError("模型目录不存在：%s，请先下载 faster-whisper-small 到该目录" % MODEL_DIR)
    t0 = time.time()
    log("加载模型（int8，CPU）…")
    model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8",
                         cpu_threads=max(4, min(12, os.cpu_count() or 8)), num_workers=1)
    log("模型加载完成 %.1fs，开始转写…" % (time.time() - t0))

    prompt = "以下是普通话视频内容，标题《%s》，涉及投资、理财、复利、定投、标普500、财富自由等。" % meta.get("title", "")
    segments, info = model.transcribe(
        audio,
        language="zh",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        condition_on_previous_text=True,
        initial_prompt=prompt,
    )
    duration = float(getattr(info, "duration", 0) or len(audio) / 16000.0)
    rows = []
    n = 0
    last_log = time.time()
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        n += 1
        rows.append((seg.start, seg.end, text))
        now = time.time()
        if now - last_log > 15:
            pct = 100.0 * seg.end / max(duration, 1)
            log("转写 %5.1f%%  [%s] %s" % (pct, fmt_srt_time(seg.end), text[:40].replace("\n", " ")))
            last_log = now
    log("转写完成：%d 段，耗时 %.1fs" % (n, time.time() - t0))
    return rows, info


def fmt_srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 999
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_outputs(cache_dir, rows):
    txt_lines, srt_lines, tsv = [], [], []
    for i, (start, end, text) in enumerate(rows, 1):
        txt_lines.append(text)
        srt_lines.append("%d\n%s --> %s\n%s\n" % (i, fmt_srt_time(start), fmt_srt_time(end), text))
        tsv.append("%s\t%s\t%s" % (fmt_srt_time(start), fmt_srt_time(end), text))
    paths = {
        "transcriptPath": os.path.join(cache_dir, "transcript.txt"),
        "srtPath": os.path.join(cache_dir, "transcript.srt"),
        "tsvPath": os.path.join(cache_dir, "transcript.tsv"),
    }
    with open(paths["transcriptPath"], "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))
    with open(paths["srtPath"], "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))
    with open(paths["tsvPath"], "w", encoding="utf-8") as f:
        f.write("\n".join(tsv))
    return paths, "\n".join(txt_lines)


# ---------------- 主流程 ----------------
def cmd_transcribe(args):
    ensure_dirs()
    bvid = parse_bvid(args.url)
    cache_dir = os.path.join(CACHE, bvid)
    os.makedirs(cache_dir, exist_ok=True)
    result_file = os.path.join(cache_dir, "result.json")

    if os.path.exists(result_file) and not args.force and not args.clip:
        try:
            with open(result_file, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("segments") and os.path.exists(cached.get("transcriptPath", "")):
                cached["cached"] = True
                cached["ok"] = True
                log("命中缓存：%s" % cache_dir)
                emit(cached)
                return 0
        except Exception:  # noqa: BLE001
            pass

    t_all = time.time()
    log("获取视频信息 %s …" % bvid)
    meta = fetch_meta(bvid)
    meta["tags"] = fetch_tags(bvid)
    log("《%s》 UP：%s 时长：%s" % (meta["title"], meta["owner"], meta["durationText"]))

    audio_path = os.path.join(cache_dir, "audio.m4a")
    audio_info = {}
    if not (os.path.exists(audio_path) and os.path.getsize(audio_path) > 100 * 1024):
        log("下载音频…")
        audio_info = download_audio(meta, audio_path)
    else:
        audio_info = {"audioPath": audio_path,
                      "audioMB": round(os.path.getsize(audio_path) / 1048576.0, 2),
                      "quality": "cached"}

    audio = decode_audio(audio_path, clip_seconds=args.clip)
    rows, info = transcribe(audio, meta)
    if not rows:
        raise RuntimeError("没有识别到任何语音内容（视频可能是纯音乐或静音）")

    paths, full_text = write_outputs(cache_dir, rows)
    result = {
        "ok": True,
        "bvid": meta["bvid"],
        "title": meta["title"],
        "owner": meta["owner"],
        "durationSec": meta["durationSec"],
        "durationText": meta["durationText"],
        "pubdate": meta["pubdate"],
        "views": meta["stats"]["view"],
        "likes": meta["stats"]["like"],
        "coins": meta["stats"]["coin"],
        "tags": meta["tags"],
        "audioPath": audio_info.get("audioPath"),
        "audioMB": audio_info.get("audioMB"),
        "transcriptPath": paths["transcriptPath"],
        "srtPath": paths["srtPath"],
        "tsvPath": paths["tsvPath"],
        "cacheDir": cache_dir,
        "segments": len(rows),
        "model": os.path.basename(MODEL_DIR),
        "elapsedSec": round(time.time() - t_all, 1),
        "cached": False,
        "clip": args.clip or 0,
        "text": full_text,
    }
    if not args.clip:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    log("全部完成：%.1fs" % result["elapsedSec"])
    emit(result)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("--url", required=True)
    t.add_argument("--force", action="store_true")
    t.add_argument("--clip", type=int, default=0, help="只转写前 N 秒（调试用，不写缓存）")
    args = ap.parse_args()
    try:
        if args.cmd == "transcribe":
            return cmd_transcribe(args)
    except Exception as e:  # noqa: BLE001
        log("ERROR: %s" % e)
        emit({"ok": False, "error": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())

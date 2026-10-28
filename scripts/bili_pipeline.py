# -*- coding: utf-8 -*-
"""B 站视频本地转写流水线（DSH 插件 dsh-local-bili-summary 的后端）。

用法:
  python bili_pipeline.py transcribe --url <b站链接或BV号> [--force] [--clip 秒数]
                                     [--fast] [--frames N]

行为:
  1. 解析 BV 号，调 B 站公开 API 取元数据（免登录）
  2. 取 DASH 音轨（码率最高一路），带 UA/Referer 下载到缓存目录
  3. ffprobe 体检音轨时长，和接口时长对不上就重下一次（防半截文件）
  4. ffmpeg 解码成 16kHz 单声道 float32（走 f32le 管道，不解码整段到磁盘），没装 ffmpeg 时回退 PyAV
  5. faster-whisper(int8) 本地转写，输出 txt / srt / tsv
  6. 需要时用 ffmpeg 按时间点抽帧，拼成 contact sheet（PPT / 图表 / 演示类视频的救命稻草）
  7. 末尾向 stdout 打印一行 RESULT_JSON:{...} 供 Node 端解析

环境变量:
  BILI_SUMMARY_HOME        数据根目录（默认 ~/.dsh/bili-video-summary）
  BILI_SUMMARY_PYLIBS      python 依赖目录（默认 $HOME/pylibs）
  BILI_SUMMARY_MODEL       模型目录（默认 $HOME/model/faster-whisper-small）
  BILI_SUMMARY_FFMPEG      ffmpeg.exe 绝对路径（可选；默认自动找 PATH / C:\\ffmpeg\\*\\bin）
  BILI_SUMMARY_FFPROBE     同上，ffprobe
  BILI_SUMMARY_FFMPEG_DIR  只给目录也行（目录下需有 ffmpeg/ffprobe）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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

EXE = ".exe" if os.name == "nt" else ""
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_FF_CACHE = {}


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


# ---------------- ffmpeg / ffprobe 定位 ----------------
def find_tool(name):
    """按 环境变量 → PATH → 常见安装目录 找 ffmpeg / ffprobe，找不到返回 None。"""
    if name in _FF_CACHE:
        return _FF_CACHE[name]
    found = None
    env_direct = os.environ.get("BILI_SUMMARY_" + name.upper())
    if env_direct and os.path.isfile(env_direct):
        found = env_direct
    if not found:
        env_dir = os.environ.get("BILI_SUMMARY_FFMPEG_DIR")
        if env_dir:
            cand = os.path.join(env_dir, name + EXE)
            if os.path.isfile(cand):
                found = cand
    if not found:
        found = shutil.which(name)
    if not found:
        bases = [
            r"C:\ffmpeg",
            r"D:\ruanjian\luobolive\ffmpeg",
            os.path.join(os.path.expanduser("~"), "ffmpeg"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg"),
        ]
        for base in bases:
            if not os.path.isdir(base):
                continue
            cand = os.path.join(base, name + EXE)
            if os.path.isfile(cand):
                found = cand
                break
            cand = os.path.join(base, "bin", name + EXE)
            if os.path.isfile(cand):
                found = cand
                break
            try:
                subs = sorted(os.listdir(base))
            except OSError:
                subs = []
            for sub in subs:
                cand = os.path.join(base, sub, "bin", name + EXE)
                if os.path.isfile(cand):
                    found = cand
                    break
            if found:
                break
    _FF_CACHE[name] = found
    return found


def run_tool(exe, args, timeout=1800, nostdin=True):
    cmd = [exe, "-hide_banner"] + (["-nostdin"] if nostdin else []) + args
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=NO_WINDOW, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError("%s 退出码 %d：%s" % (os.path.basename(exe), p.returncode, " / ".join(tail)))
    return p.stdout


def ffmpeg_version(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=30).stdout
        first = out.decode("utf-8", "replace").splitlines()[0]
        return first.replace("ffmpeg version ", "").split(" Copyright")[0].strip()
    except Exception:  # noqa: BLE001
        return "unknown"


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


def fetch_play(bvid, cid):
    data = http_json(API_PLAY.format(bvid=bvid, cid=cid))
    if data.get("code") != 0:
        raise RuntimeError("取播放地址失败：%s %s" % (data.get("code"), data.get("message")))
    return data["data"]


# ---------------- 音频 ----------------
def download_audio(meta, audio_path):
    play = fetch_play(meta["bvid"], meta["cid"])
    urls = []
    dash = play.get("dash") or {}
    if dash.get("audio"):
        best = max(dash["audio"], key=lambda a: a.get("bandwidth") or 0)
        urls.append(best["baseUrl"])
        urls.extend(best.get("backupUrl") or [])
        quality = "%d kbps" % int((best.get("bandwidth") or 0) / 1000)
    else:
        # 极少数视频只有 durl：取 mp4，ffmpeg / av 都能解出音轨
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


def probe_audio(audio_path):
    """ffprobe 体检：时长 / 码率 / 编码。装不上 ffprobe 就返回空 dict。"""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return {}
    try:
        out = run_tool(ffprobe, ["-v", "error", "-select_streams", "a:0",
                                 "-show_entries",
                                 "stream=codec_name,sample_rate,channels,bit_rate:format=duration,format_name",
                                 "-of", "json", audio_path], timeout=120, nostdin=False)
        data = json.loads(out.decode("utf-8", "replace") or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        return {
            "codec": stream.get("codec_name"),
            "sampleRate": int(stream.get("sample_rate") or 0),
            "channels": int(stream.get("channels") or 0),
            "kbps": int(round(int(stream.get("bit_rate") or fmt.get("bit_rate") or 0) / 1000.0)),
            "durationSec": round(float(fmt.get("duration") or 0), 1),
        }
    except Exception as e:  # noqa: BLE001
        log("ffprobe 体检失败（不影响转写）：%s" % e)
        return {}


def audio_ok(probe, meta, clip_seconds=0):
    """下载完整性判断：音轨时长和接口时长差超过 3 秒 / 3% 就认为是一次坏下载。"""
    if clip_seconds or not probe or not probe.get("durationSec"):
        return True
    api = float(meta.get("durationSec") or 0)
    got = float(probe.get("durationSec") or 0)
    if not api:
        return True
    return abs(api - got) <= max(3.0, api * 0.03)


# ---------------- 解码 ----------------
def ensure_pylibs():
    if PYLIBS not in sys.path:
        sys.path.insert(0, PYLIBS)


def decode_audio(audio_path, clip_seconds=0):
    """优先 ffmpeg（f32le 管道，不落盘），失败回退 PyAV。"""
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg:
        try:
            return decode_audio_ffmpeg(ffmpeg, audio_path, clip_seconds)
        except Exception as e:  # noqa: BLE001
            log("ffmpeg 解码失败，回退 PyAV：%s" % e)
    return decode_audio_pyav(audio_path, clip_seconds)


def decode_audio_ffmpeg(ffmpeg, audio_path, clip_seconds=0):
    import numpy as np

    t0 = time.time()
    args = []
    if clip_seconds:
        args += ["-ss", "0", "-t", str(int(clip_seconds))]
    args += ["-i", audio_path, "-vn", "-ac", "1", "-ar", "16000",
             "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    raw = run_tool(ffmpeg, args, timeout=1800)
    audio = np.frombuffer(raw, dtype="<f4").copy()
    if clip_seconds:
        audio = audio[: int(clip_seconds) * 16000]
    if audio.size == 0:
        raise RuntimeError("ffmpeg 解出 0 个采样点")
    log("解码完成（ffmpeg）：%.1f 秒音频，耗时 %.2fs" % (len(audio) / 16000.0, time.time() - t0))
    return audio


def decode_audio_pyav(audio_path, clip_seconds=0):
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
    log("解码完成（PyAV）：%.1f 秒音频，耗时 %.1fs" % (len(audio) / 16000.0, time.time() - t0))
    return audio


# ---------------- 抽帧 ----------------
GRID_ORDER = [(2, 1), (2, 2), (3, 2), (4, 2), (3, 3), (4, 3), (4, 4), (5, 4)]


def grid_for(n):
    for cols, rows in GRID_ORDER:
        if cols * rows >= n:
            return cols, rows
    return GRID_ORDER[-1]


def pick_video_stream(play):
    """挑一路视频流抽帧：优先 ≤720p 里最高的，同高度优先 avc1（解码更快）。"""
    dash = play.get("dash") or {}
    vids = [v for v in (dash.get("video") or []) if v.get("baseUrl")]
    if vids:
        cand = [v for v in vids if (v.get("height") or 0) <= 720] or vids
        best = max(cand, key=lambda v: ((v.get("height") or 0),
                                        1 if str(v.get("codecs") or "").startswith("avc") else 0,
                                        v.get("bandwidth") or 0))
        return {
            "url": best["baseUrl"],
            "backup": [u for u in (best.get("backupUrl") or []) if u],
            "height": best.get("height") or 0,
            "codecs": best.get("codecs") or "",
            "bandwidth": best.get("bandwidth") or 0,
        }
    for item in play.get("durl") or []:
        if item.get("url"):
            return {"url": item["url"], "backup": [], "height": 0, "codecs": "mp4", "bandwidth": 0}
    return None


def _grab_one(ffmpeg, stream, t, out_path):
    """抓一帧。CDN 偶尔会掐并发连接，这里自带换备用地址 + 重试。"""
    args_head = ["-headers", "Referer: https://www.bilibili.com/\r\nUser-Agent: %s\r\n" % UA,
                 "-ss", "%.2f" % t]
    tail = ["-frames:v", "1"]
    if stream.get("height") and stream["height"] < 480:
        tail += ["-vf", "scale=iw*2:ih*2"]  # 太糊的小图放大一倍，方便读屏文
    tail += ["-y", out_path]
    last = None
    for url in [stream["url"]] + (stream.get("backup") or [])[:1]:
        for attempt in range(2):
            try:
                run_tool(ffmpeg, args_head + ["-i", url] + tail, timeout=120)
                return True
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.4 + attempt)
    log("抓帧失败 @%ss：%s" % (t, str(last)[:200]))
    return False


def _grab_batch(ffmpeg, stream, items, workers):
    """items: [(t, path), ...]；返回失败的下标列表。"""
    failed = []

    def job(idx, t, path):
        if not _grab_one(ffmpeg, stream, t, path):
            failed.append(idx)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(lambda a: job(*a), [(i, t, p) for i, (t, p) in enumerate(items)]))
    return sorted(failed)


def grab_frames(play, meta, cache_dir, want):
    """按时间点抽 want 张帧，拼成 contact sheet。返回结果字段；不支持时返回 {}。"""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        log("没找到 ffmpeg，跳过抽帧")
        return {}
    stream = pick_video_stream(play)
    if not stream:
        log("没有可用的视频流，跳过抽帧")
        return {}

    want = max(1, min(int(want or 0), 20))
    cols, rows = grid_for(want)
    count = min(cols * rows, 20)
    dur = float(meta.get("durationSec") or 0) or 1.0
    times = [round(dur * (0.04 + 0.92 * (i / max(count - 1, 1))), 2) for i in range(count)]

    out_dir = os.path.join(cache_dir, "frames")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    paths = [os.path.join(out_dir, "frame-%02d.png" % (i + 1)) for i in range(count)]

    t0 = time.time()
    failed = _grab_batch(ffmpeg, stream, list(zip(times, paths)), workers=4)
    if failed:
        log("并发抓帧失败 %d 张，刷新地址后串行重试" % len(failed))
        try:
            fresh = pick_video_stream(fetch_play(meta["bvid"], meta["cid"]))
            if fresh:
                stream = fresh
        except Exception:  # noqa: BLE001
            pass
        failed = _grab_batch(ffmpeg, stream, [(times[i], paths[i]) for i in failed], workers=1)
    got = [p for p in paths if os.path.exists(p) and os.path.getsize(p) > 1000]
    if not got:
        log("抽帧全部失败，跳过")
        return {}
    log("抽帧完成：%d/%d 张，耗时 %.1fs" % (len(got), count, time.time() - t0))

    sheet = os.path.join(out_dir, "sheet.png")
    if len(got) == count:
        try:
            run_tool(ffmpeg, ["-framerate", "1", "-start_number", "1",
                              "-i", os.path.join(out_dir, "frame-%02d.png"),
                              "-vf", "scale=560:-2,tile=%dx%d:margin=8:padding=8:color=white" % (cols, rows),
                              "-frames:v", "1", "-update", "1", "-y", sheet], timeout=300)
        except Exception as e:  # noqa: BLE001
            log("拼图失败（单帧仍在）：%s" % e)
    if not os.path.isfile(sheet):
        sheet = ""

    got_times = [t for t, p in zip(times, paths) if p in got]
    return {
        "frameSheet": sheet,
        "frames": got,
        "frameTimes": got_times,
        "frameGrid": "%dx%d" % (cols, rows),
        "videoStream": "h=%s %s %dkbps" % (stream.get("height") or "?", stream.get("codecs") or "?",
                                           int((stream.get("bandwidth") or 0) / 1000)),
        "framesSec": round(time.time() - t0, 1),
    }


# ---------------- 转写 ----------------
def transcribe(audio, meta, beam_size=5):
    ensure_pylibs()
    from faster_whisper import WhisperModel

    if not os.path.isdir(MODEL_DIR):
        raise RuntimeError("模型目录不存在：%s，请先下载 faster-whisper-small 到该目录" % MODEL_DIR)
    t0 = time.time()
    log("加载模型（int8，CPU，beam=%d）…" % beam_size)
    model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8",
                         cpu_threads=max(4, min(12, os.cpu_count() or 8)), num_workers=1)
    log("模型加载完成 %.1fs，开始转写…" % (time.time() - t0))

    prompt = "以下是普通话视频内容，标题《%s》，涉及投资、理财、复利、定投、标普500、财富自由等。" % meta.get("title", "")
    segments, info = model.transcribe(
        audio,
        language="zh",
        beam_size=beam_size,
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
    beam = 1 if args.fast else 5
    want_frames = max(0, int(args.frames or 0))

    if os.path.exists(result_file) and not args.force and not args.clip:
        try:
            with open(result_file, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("segments") and os.path.exists(cached.get("transcriptPath", "")):
                # 缓存里没有帧、但这次要帧：只补抽帧，不重跑转写
                if want_frames and not cached.get("frameSheet"):
                    try:
                        meta = fetch_meta(bvid)
                        extra = grab_frames(fetch_play(meta["bvid"], meta["cid"]), meta, cache_dir, want_frames)
                        if extra:
                            cached.update(extra)
                            cached["ok"] = True
                            with open(result_file, "w", encoding="utf-8") as f:
                                json.dump(cached, f, ensure_ascii=False, indent=1)
                    except Exception as e:  # noqa: BLE001
                        log("补抽帧失败（不影响转写）：%s" % e)
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

    ffmpeg = find_tool("ffmpeg")
    ff_note = ffmpeg_version(ffmpeg) if ffmpeg else "未检测到（回退 PyAV 解码）"
    log("ffmpeg：%s" % (ffmpeg or "未找到"))

    audio_path = os.path.join(cache_dir, "audio.m4a")
    audio_info = {}
    probe = {}
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 100 * 1024:
        probe = probe_audio(audio_path)
        if args.clip or audio_ok(probe, meta):
            audio_info = {"audioPath": audio_path,
                          "audioMB": round(os.path.getsize(audio_path) / 1048576.0, 2),
                          "quality": "cached"}
        else:
            log("缓存音轨时长对不上（接口 %ss / 实际 %ss），重新下载"
                % (meta.get("durationSec"), probe.get("durationSec")))
    if not audio_info:
        log("下载音频…")
        audio_info = download_audio(meta, audio_path)
        probe = probe_audio(audio_path) or probe
        if not args.clip and not audio_ok(probe, meta):
            log("下载后时长仍对不上（接口 %ss / 实际 %ss），按实际时长继续"
                % (meta.get("durationSec"), probe.get("durationSec")))

    play = None
    frames_info = {}
    if want_frames:
        try:
            play = fetch_play(meta["bvid"], meta["cid"])
            frames_info = grab_frames(play, meta, cache_dir, want_frames)
        except Exception as e:  # noqa: BLE001
            log("抽帧失败（不影响转写）：%s" % e)

    t_dec = time.time()
    audio = decode_audio(audio_path, clip_seconds=args.clip)
    decode_sec = round(time.time() - t_dec, 2)

    rows, info = transcribe(audio, meta, beam_size=beam)
    if not rows:
        raise RuntimeError("没有识别到任何语音内容（视频可能是纯音乐或静音）")

    # 试转（--clip）的输出写 work 目录，别覆盖缓存里的完整转录
    out_dir = cache_dir
    if args.clip:
        out_dir = os.path.join(WORK, "clip-%s-%ss" % (meta["bvid"], args.clip))
        os.makedirs(out_dir, exist_ok=True)
    paths, full_text = write_outputs(out_dir, rows)
    result = {
        "ok": True,
        "bvid": meta["bvid"],
        "cid": meta["cid"],
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
        "audioProbe": probe,
        "ffmpeg": ff_note,
        "decodeSec": decode_sec,
        "beam": beam,
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
    result.update(frames_info)
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
    t.add_argument("--fast", action="store_true", help="beam_size=1，速度约快 2.4 倍，中文识别质量基本不变")
    t.add_argument("--frames", type=int, default=0, help="按时间点抽 N 帧（PPT / 图表类视频用），0 表示不抽")
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

/**
 * dsh-local-bili-summary：B 站视频一键总结（本地转写 + 出图 + 出 md）。
 *
 * 注册两个模型可见工具：
 *   bili_video_transcribe —— 给链接/BV 号，本地下载音轨 + faster-whisper 转写，返回元数据与全文
 *   bili_video_render     —— 给结构化内容，渲染「一图读懂」PNG 并写 md 文档
 *
 * 依赖（已固定在 ~/.dsh/bili-video-summary）：
 *   pylibs/           faster-whisper / ctranslate2 / av / onnxruntime
 *   model/…-small/    faster-whisper-small int8
 * python 解释器：优先 DSH 自带运行时，其次环境变量 BILI_SUMMARY_PYTHON，最后 PATH 里的 python。
 */
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'tool-bili-summary';
export const inject = ['tools'];

const PKG_DIR = dirname(fileURLToPath(import.meta.url));
const SCRIPT_TRANSCRIBE = join(PKG_DIR, 'scripts', 'bili_pipeline.py');
const SCRIPT_RENDER = join(PKG_DIR, 'scripts', 'render_summary.py');
const DATA_HOME = process.env.BILI_SUMMARY_HOME || join(homedir(), '.dsh', 'bili-video-summary');
const WORK_DIR = join(DATA_HOME, 'work');

function pythonPath() {
  if (process.env.BILI_SUMMARY_PYTHON) return process.env.BILI_SUMMARY_PYTHON;
  const bundled = join(homedir(), '.dsh', 'dsh-runtimes', 'dsh-primary-runtime',
    'dependencies', 'python', 'python.exe');
  if (existsSync(bundled)) return bundled;
  return process.platform === 'win32' ? 'python.exe' : 'python3';
}

function runPython(args, timeoutMs) {
  return new Promise((resolve, reject) => {
    const py = pythonPath();
    if (!existsSync(py) && py.includes('python.exe')) {
      reject(new Error(`找不到 Python 解释器：${py}`));
      return;
    }
    const child = spawn(py, args, {
      cwd: DATA_HOME,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', BILI_SUMMARY_HOME: DATA_HOME },
    });
    let stdout = '';
    let stderr = '';
    let moved = false;
    const timer = setTimeout(() => {
      moved = true;
      try { child.kill(); } catch { /* ignore */ }
      reject(new Error(`处理超时（${Math.round(timeoutMs / 60000)} 分钟），可以调小视频或稍后重试`));
    }, timeoutMs);
    child.stdout.on('data', (buf) => { stdout += buf.toString('utf8'); });
    child.stderr.on('data', (buf) => {
      stderr += buf.toString('utf8');
      if (stderr.length > 20000) stderr = stderr.slice(-20000);
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      reject(new Error(`无法启动 Python：${err.message}`));
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (moved) return;
      const line = stdout.split(/\r?\n/).reverse().find((l) => l.startsWith('RESULT_JSON:'));
      if (!line) {
        const tail = (stderr || '').trim().split(/\r?\n/).slice(-8).join('\n');
        reject(new Error(`转写脚本失败（exit ${code}）${tail ? '：\n' + tail : ''}`));
        return;
      }
      let payload;
      try {
        payload = JSON.parse(line.slice('RESULT_JSON:'.length));
      } catch (err) {
        reject(new Error(`解析脚本输出失败：${err.message}`));
        return;
      }
      if (!payload.ok) {
        reject(new Error(payload.error || '处理失败'));
        return;
      }
      resolve(payload);
    });
  });
}

/* ---------------- 工具 1：转写 ---------------- */
const TRANSCRIBE_PARAMS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    url: {
      type: 'string',
      description: 'B 站视频链接（https://www.bilibili.com/video/BV…）或 12 位 BV 号。',
    },
    force: {
      type: 'boolean',
      description: 'true 时忽略本地缓存重新下载和转写；默认 false，重复视频会秒回缓存。',
    },
    frames: {
      type: 'integer',
      minimum: 0,
      maximum: 20,
      description:
        '用 ffmpeg 按时间点抽多少帧并拼成 contact sheet（PPT / 图表 / 软件演示 / 教程类视频强烈建议 8–12）。'
        + '返回 frameSheet（拼图）和 frames（单帧）路径，可用 read_image 读图，用来校正 ASR 听错的插件名 / 产品名 / 界面文案。'
        + '默认 8；传 0 关闭（纯口播 / 播客类视频可以关掉，省 10 秒左右）。',
    },
    fast: {
      type: 'boolean',
      description:
        'true 时用 beam_size=1，转写速度约快 2.4 倍（实测 3 分钟视频 73s → 36s），中文识别质量基本不变。'
        + '长视频、只要大意时建议开启；要抠字眼 / 数字时用默认的 false。',
    },
  },
  required: ['url'],
};

const OUTPUT_SCHEMA = { type: 'object', additionalProperties: true };

function renderTranscribe(_args, v) {
  const lines = [
    `《${v.title}》 UP：${v.owner} · ${v.durationText} · ${v.pubdate}`,
    `播放 ${v.views} / 点赞 ${v.likes} / 投币 ${v.coins}${v.tags?.length ? ' · 标签：' + v.tags.join('、') : ''}`,
    `转录 ${v.segments} 段，耗时 ${v.elapsedSec}s（beam=${v.beam ?? 5}，解码 ${v.decodeSec ?? '-'}s），模型 ${v.model}${v.cached ? '（命中缓存）' : ''}；文件：`,
    `  txt: ${v.transcriptPath}`,
    `  srt: ${v.srtPath}`,
  ];
  if (v.frameSheet) {
    lines.push(`  帧拼图: ${v.frameSheet}（${v.frameTimes?.length ?? 0} 帧 @ ${(v.frameTimes || []).join('s, ')}s，读图可校正专名）`);
  } else if (v.frames?.length) {
    lines.push(`  单帧: ${v.frames.join(' , ')}`);
  }
  if (v.audioProbe?.durationSec) {
    lines.push(`  音轨: ${v.audioProbe.codec} ${v.audioProbe.kbps}kbps ${v.audioProbe.durationSec}s / ffmpeg ${v.ffmpeg}`);
  }
  lines.push('', '转录正文（引用前注意：ASR 可能有数字/专名误差，有帧时优先以帧里的屏文为准）：', v.text);
  return [{ type: 'text', text: lines.join('\n') }];
}

/* ---------------- 工具 2：渲染 ---------------- */
const RENDER_PARAMS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    payload: {
      type: 'object',
      additionalProperties: true,
      description:
        '渲染所需的全部内容。示例：{ "bvid":"BV1xx","outDir":"F:/.../output/bilibili-xxx", '
        + '"md":"# 标题\\n...markdown 全文...", "mdFileName":"解析-xxx.md", "pngFileName":"视频总结一图读懂.png", '
        + '"copyTranscripts":[".../transcript.txt",".../transcript.srt",".../frames/sheet.png"], '
        + '"plan":{ ... } }。plan 有两种版式：'
        + '① 叙事版（无 cards 字段）：pitfalls[] + goal/milestones[] + suggestions[] + caveats[] + todos[]，适合观点、理财、人生建议类；'
        + '② 卡片版（有 cards 字段，榜单 / 插件 / 清单类首选）：'
        + 'kicker、title、titleHl（标题里要变蓝的那几个字）、subtitle、stats:[{value,label}]、quoteLabel、quote、'
        + 'cards:[{num,en,zh,color,desc,bullets[]}]（color 取 blue/amber/rose/violet/green/red，整图 4 列自动排布、高度按内容自适应）、'
        + 'panels:[{title,color,ordered,items[]}]（底部 3 块，如装机顺序 / 谁最该用 / 打个折扣）、footer。',
    },
  },
  required: ['payload'],
};

function renderRender(_args, v) {
  const lines = [
    v.ok ? '已生成：' : '生成失败：',
    `  图：${v.pngPath}`,
    v.mdPath ? `  md：${v.mdPath}` : '',
    `  目录：${v.outDir}`,
  ].filter(Boolean);
  return [{ type: 'text', text: lines.join('\n') }];
}

export function apply(ctx) {
  ctx.tools.register({
    name: 'bili_video_transcribe',
    description:
      'Download a Bilibili video locally and transcribe it with faster-whisper (ffmpeg handles decode + keyframe sampling). '
      + 'Use when the user shares a bilibili.com/video/BV... link or asks to summarize/parse a B station video. '
      + 'First call is slow (roughly 0.25x video duration with fast=true, 0.4x with the default beam 5); '
      + 'repeat calls hit the local cache and return instantly. '
      + 'Returns metadata (title, uploader, duration, views, tags), transcript file paths, the full transcript text, '
      + 'and — when frames>0 — a contact sheet PNG plus individual frames so you can read on-screen text '
      + 'and fix names the ASR got wrong. '
      + 'After transcribing, write the analysis yourself, then call bili_video_render to produce the markdown + infographic.',
    parameters: TRANSCRIBE_PARAMS,
    output: { schema: OUTPUT_SCHEMA, render: renderTranscribe },
    async execute(args) {
      const cmd = ['transcribe', '--url', String(args.url || '')];
      if (args.force) cmd.push('--force');
      if (args.fast) cmd.push('--fast');
      const frames = args.frames === undefined ? 8 : Number(args.frames) || 0;
      if (frames > 0) cmd.push('--frames', String(frames));
      return runPython([SCRIPT_TRANSCRIBE, ...cmd], 60 * 60 * 1000);
    },
  });

  ctx.tools.register({
    name: 'bili_video_render',
    description:
      'Render the Bilibili video summary deliverables: write the markdown file and draw a one-page infographic PNG. '
      + 'Call it after bili_video_transcribe and after you finish writing the analysis. '
      + 'Pass a payload object with: outDir (directory to write into), md (full markdown text), '
      + 'mdFileName / pngFileName (optional), copyTranscripts (optional file paths to copy — transcript txt/srt 以及 '
      + 'frameSheet / frames 里的 PNG 都可以放进来，按 basename 平铺复制到 outDir), '
      + 'and plan (structured infographic content: title, subtitle, quote, pitfalls, goal + 3 milestones, '
      + 'up to 5 suggestions, caveats, todos, footer). '
      + 'Returns the absolute paths of the generated PNG and markdown files.',
    parameters: RENDER_PARAMS,
    output: { schema: OUTPUT_SCHEMA, render: renderRender },
    async execute(args) {
      const payload = args.payload || {};
      if (!payload.plan) throw new Error('payload.plan 不能为空');
      mkdirSync(WORK_DIR, { recursive: true });
      const planFile = join(WORK_DIR, `render-${Date.now()}.json`);
      writeFileSync(planFile, JSON.stringify(payload, null, 2), 'utf8');
      return runPython([SCRIPT_RENDER, '--plan-file', planFile], 10 * 60 * 1000);
    },
  });
}

export default { name, inject, apply };

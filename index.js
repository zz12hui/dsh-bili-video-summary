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
  },
  required: ['url'],
};

const OUTPUT_SCHEMA = { type: 'object', additionalProperties: true };

function renderTranscribe(_args, v) {
  const lines = [
    `《${v.title}》 UP：${v.owner} · ${v.durationText} · ${v.pubdate}`,
    `播放 ${v.views} / 点赞 ${v.likes} / 投币 ${v.coins}${v.tags?.length ? ' · 标签：' + v.tags.join('、') : ''}`,
    `转录 ${v.segments} 段，耗时 ${v.elapsedSec}s，模型 ${v.model}${v.cached ? '（命中缓存）' : ''}；文件：`,
    `  txt: ${v.transcriptPath}`,
    `  srt: ${v.srtPath}`,
    '',
    '转录正文（引用前注意：ASR 可能有数字/专名误差）：',
    v.text,
  ];
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
        + '"copyTranscripts":[".../transcript.txt",".../transcript.srt"], '
        + '"plan":{ "title":"一张图读懂《…》","subtitle":"UP · 时长 · 播放 · 日期","quote":"一句话主旨",'
        + '"pitfallsTitle":"他年轻时踩过的坑","pitfalls":["…","…","…"],"goalTitle":"目标",'
        + '"goalBasis":"按 … 的算例","milestones":[{"amount":"20 万","tag":"灯塔 1 · 第 8 年",'
        + '"desc":"…","color":"blue","connector":"再 7 年"}],"milestoneNote":"…",'
        + '"suggestionsTitle":"…","suggestionsHint":"…","suggestions":[{"title":"…","bullets":["…","…"]}],'
        + '"caveatsTitle":"打个折扣","caveats":["…"],"todosTitle":"可直接抄的 4 件事","todos":["…"],'
        + '"footer":"来源…" } }。最多 5 条建议、4 条里程碑、5 条踩坑/提醒/清单。',
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
      'Download a Bilibili video\'s audio and transcribe it locally with faster-whisper. '
      + 'Use when the user shares a bilibili.com/video/BV... link or asks to summarize/parse a B station video. '
      + 'First call is slow (roughly 0.4x video duration on CPU, e.g. 12 min for a 30-min video); '
      + 'repeat calls hit the local cache and return instantly. '
      + 'Returns metadata (title, uploader, duration, views, tags), transcript file paths, and the full transcript text. '
      + 'After transcribing, write the analysis yourself, then call bili_video_render to produce the markdown + infographic.',
    parameters: TRANSCRIBE_PARAMS,
    output: { schema: OUTPUT_SCHEMA, render: renderTranscribe },
    async execute(args) {
      const cmd = ['transcribe', '--url', String(args.url || '')];
      if (args.force) cmd.push('--force');
      return runPython([SCRIPT_TRANSCRIBE, ...cmd], 60 * 60 * 1000);
    },
  });

  ctx.tools.register({
    name: 'bili_video_render',
    description:
      'Render the Bilibili video summary deliverables: write the markdown file and draw a one-page infographic PNG. '
      + 'Call it after bili_video_transcribe and after you finish writing the analysis. '
      + 'Pass a payload object with: outDir (directory to write into), md (full markdown text), '
      + 'mdFileName / pngFileName (optional), copyTranscripts (optional transcript file paths to copy), '
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

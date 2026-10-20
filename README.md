# dsh-local-bili-summary

DSH 本地插件：在会话里发一个 B 站视频链接，自动完成「本地转写 → 出解析 md → 出一图读懂 PNG」。

不依赖 B 站登录、不调用第三方字幕服务，音频和转写全部在本机完成。

## 两个工具（模型可见）

| 工具 | 作用 | 耗时 |
|---|---|---|
| `bili_video_transcribe` | 给链接或 BV 号，下载音轨 + faster-whisper 本地转写，返回元数据、全文和 txt/srt/tsv 路径 | 首次约视频时长的 0.4 倍（30 分钟视频约 12 分钟），重复视频命中缓存秒回 |
| `bili_video_render` | 给结构化内容，渲染 1920×1320 的「一图读懂」PNG，并把 md 一起写进输出目录 | 1–2 秒 |

典型调用顺序：`bili_video_transcribe` → 模型读转录写解析 → `bili_video_render`。

## 安装位置

| 内容 | 路径 |
|---|---|
| 插件本体 | `C:\Users\Administrator\.dsh\profiles\desktop\node_modules\dsh-local-bili-summary\` |
| profile 补丁行 | `C:\Users\Administrator\.dsh\profiles\desktop\cordis.patch.yml` 末尾 `bili-summary-panel` |
| 数据根目录 | `C:\Users\Administrator\.dsh\bili-video-summary\` |
| python 依赖 | `…\bili-video-summary\pylibs\`（faster-whisper 1.2.1 / ctranslate2 4.8.3 / av 19 / onnxruntime） |
| 模型 | `…\bili-video-summary\model\faster-whisper-small\`（int8，约 462MB） |
| 缓存 | `…\bili-video-summary\cache\<BV号>\`（audio.m4a / transcript.* / result.json） |

插件在 DSH **启动时**加载，装完/改完要重启 DSH（含托盘退出）才生效；刷新页面不会加载 host 插件。

## 为什么这样设计（和第一次手工解析踩过的坑）

1. **不走网页抓取**：B 站页面是 JS 外壳，直接 fetch 拿不到内容；插件只用公开 JSON API，带 UA/Referer，免登录。
2. **不等官方字幕**：匿名请求字幕接口返回 0 条，AI 总结接口要求登录（-101），所以直接下载音轨做本地 ASR。
3. **不装 yt-dlp / 不需要 ffmpeg**：`playurl` 直接给 DASH 音轨（m4a），PyAV 自带解码。
4. **模型走 ModelScope**：hf-mirror 单线程约 300KB/s、多线程触发 429；ModelScope 约 9MB/s。
5. **绕过 av 19 与 faster-whisper 的兼容问题**：自己用 `av.open` 解码成 16kHz 单声道 float32 再喂 `model.transcribe`。
6. **长任务交给插件**：Node `spawn` 管理 Python 子进程，超时 60 分钟，进度写 stderr，结果用一行 `RESULT_JSON:` 回传。
7. **按 BV 号缓存**：同一视频第二次直接读缓存，不再重复下载和转写。

完整的 16 条流程问题清单见 `references/踩坑清单.md`。

## 配置

环境变量（可选）：

| 变量 | 默认值 |
|---|---|
| `BILI_SUMMARY_HOME` | `~/.dsh/bili-video-summary` |
| `BILI_SUMMARY_PYLIBS` | `$BILI_SUMMARY_HOME/pylibs` |
| `BILI_SUMMARY_MODEL` | `$BILI_SUMMARY_HOME/model/faster-whisper-small` |
| `BILI_SUMMARY_PYTHON` | DSH 自带 python 运行时 |

## 一键装环境（换机器 / 给别人用）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
```

脚本会建好 `~/.dsh/bili-video-summary`，用阿里云镜像装 faster-whisper 到独立 `pylibs`，从 ModelScope 拉 `faster-whisper-small`（约 461MB），最后校验依赖。可传 `-Python` / `-DataHome` 换解释器和数据目录。

## 手动跑（排查用）

```powershell
$py = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
# 转写（--force 忽略缓存；--clip 60 只转前 60 秒，调试用）
& $py "...\scripts\bili_pipeline.py" transcribe --url "https://www.bilibili.com/video/BVxxxx" 
# 渲染
& $py "...\scripts\render_summary.py" --plan-file plan.json
```

## 回滚

删掉 `cordis.patch.yml` 末尾的 `bili-summary-panel` 插入块和 `node_modules\dsh-local-bili-summary`，重启 DSH；数据目录可保留也可整目录删除。

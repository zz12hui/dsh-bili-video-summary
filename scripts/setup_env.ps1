# 一键准备 dsh-local-bili-summary 的运行环境（Windows / PowerShell）
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1 -Python "C:\path\to\python.exe" -DataHome "D:\bili-data"
#
# 做的事:
#   1. 建数据目录（model / pylibs / cache / work）
#   2. 用阿里云 pypi 镜像安装 faster-whisper 到独立 pylibs（不污染系统环境）
#   3. 从 ModelScope 下载 faster-whisper-small（约 461MB；hf-mirror 限速严重，别用）
#   4. 校验依赖可导入
param(
  [string]$Python = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
  [string]$DataHome = "$env:USERPROFILE\.dsh\bili-video-summary"
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Python)) {
  Write-Error "找不到 python：$Python；请用 -Python 指定解释器（Python 3.10+）"
}

$Pylibs = Join-Path $DataHome 'pylibs'
$ModelDir = Join-Path $DataHome 'model\faster-whisper-small'
foreach ($d in @($DataHome, $Pylibs, (Join-Path $DataHome 'model'), $ModelDir,
                 (Join-Path $DataHome 'cache'), (Join-Path $DataHome 'work'))) {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}

Write-Host '[1/3] 安装 faster-whisper（阿里云镜像）...'
& $Python -m pip install --disable-pip-version-check --no-cache-dir -q `
  -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com `
  --target $Pylibs faster-whisper
if ($LASTEXITCODE -ne 0) { Write-Error "pip 安装失败（exit $LASTEXITCODE）" }

Write-Host '[2/3] 下载 faster-whisper-small（ModelScope，约 461MB）...'
$base = 'https://www.modelscope.cn/models/gpustack/faster-whisper-small/resolve/master/'
foreach ($f in @('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')) {
  $dst = Join-Path $ModelDir $f
  if ((Test-Path $dst) -and (Get-Item $dst).Length -gt 100 -and $f -ne 'model.bin') { continue }
  if ((Test-Path $dst) -and $f -eq 'model.bin' -and (Get-Item $dst).Length -gt 400MB) { continue }
  Write-Host "  - $f"
  & curl.exe -sS -L --retry 5 --retry-delay 2 -o $dst "$base$f"
  if ($LASTEXITCODE -ne 0) { Write-Error "下载失败：$f" }
}

Write-Host '[3/3] 校验依赖...'
& $Python -c "import sys; sys.path.insert(0, r'$Pylibs'); import faster_whisper, ctranslate2, av, onnxruntime; print('OK faster-whisper', faster_whisper.__version__)"
if ($LASTEXITCODE -ne 0) { Write-Error '依赖导入失败' }

Write-Host ''
Write-Host '完成。数据目录：' $DataHome
Write-Host '提示：把插件目录放进 ~/.dsh/profiles/desktop/node_modules/，并在 cordis.patch.yml 末尾加插入块，重启 DSH 生效。'

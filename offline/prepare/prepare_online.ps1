# ─────────────────────────────────────────────────────────────────────────────
# [인터넷이 되는 Windows PC에서 1회 실행] 폐쇄망 반입용 추가 파일 채우기
#   powershell -ExecutionPolicy Bypass -File .\prepare\prepare_online.ps1          (전체)
#   powershell -ExecutionPolicy Bypass -File .\prepare\prepare_online.ps1 -NoVector (벡터 DB 미사용)
#
# 필요: Python 3.9+ (python 명령), 인터넷. 결과: wheels\py39·py311·py312, models\, windows\
# ─────────────────────────────────────────────────────────────────────────────
param([switch]$NoVector)
$ErrorActionPreference = "Stop"
$B = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..")
$pkgs = @("cryptography"); if (-not $NoVector) { $pkgs += "fastembed" }
$PyWin = "3.12.7"
$Model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
$plats = @("manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_28_x86_64", "manylinux_2_27_x86_64", "manylinux_2_24_x86_64", "linux_x86_64") | ForEach-Object { "--platform"; $_ }

foreach ($v in @("3.9", "3.11", "3.12")) {
  $t = Join-Path $B ("wheels\py" + $v.Replace(".", ""))
  New-Item -ItemType Directory -Force $t | Out-Null
  Write-Host "== Linux x86_64 휠: Python $v -> $t"
  python -m pip download -q --only-binary=:all: --implementation cp --python-version $v @plats -d $t @pkgs
  if ($LASTEXITCODE -ne 0) { throw "pip download 실패 (Python $v)" }
}

Write-Host "== Windows Agent: Python $PyWin 설치 파일 + cryptography 휠"
New-Item -ItemType Directory -Force (Join-Path $B "windows\wheels") | Out-Null
Invoke-WebRequest "https://www.python.org/ftp/python/$PyWin/python-$PyWin-amd64.exe" -OutFile (Join-Path $B "windows\python-$PyWin-amd64.exe") -UseBasicParsing
python -m pip download -q --only-binary=:all: --implementation cp --python-version 3.12 --platform win_amd64 -d (Join-Path $B "windows\wheels") cryptography

if (-not $NoVector) {
  Write-Host "== 임베딩 모델: $Model -> models\"
  $tv = Join-Path $env:TEMP ("fe-" + [guid]::NewGuid())
  python -m venv $tv
  & "$tv\Scripts\python.exe" -m pip install -q fastembed
  $code = "import sys`nfrom fastembed import TextEmbedding`nm=TextEmbedding(model_name=sys.argv[2], cache_dir=sys.argv[1])`nprint('model ok, dim =', len(next(iter(m.embed(['test'])))))"
  & "$tv\Scripts\python.exe" -c $code (Join-Path $B "models") $Model
  Remove-Item -Recurse -Force $tv
}
Get-ChildItem -Recurse -File (Join-Path $B "wheels"), (Join-Path $B "windows"), (Join-Path $B "models") -ErrorAction SilentlyContinue |
  ForEach-Object { "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), ($_.FullName.Substring($B.Path.Length + 1) -replace "\\", "/") } |
  Set-Content (Join-Path $B "SHA256SUMS.prepared") -Encoding ASCII
Write-Host "완료 — 묶음 폴더 전체를 반입 매체로 옮기세요. 반입 후 'sha256sum -c SHA256SUMS.prepared' 로 무결성 확인." -ForegroundColor Green

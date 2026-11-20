# ─────────────────────────────────────────────────────────────────────────────
# 서버 Agent 설치 (Windows Server 2016+ / IIS) — 인터넷 불필요
#   관리자 PowerShell 에서:
#   powershell -ExecutionPolicy Bypass -File .\agent_install.ps1 -Platform https://cert.corp.local -Token <발급 토큰> `
#       -Scan "C:\certs" [-WinStore "LocalMachine\My"] [-CaFile C:\certs\corp-ca.pem]
#
#   토큰: 플랫폼 [관리] 탭 '서버 Agent' 에서 서버 등록 → [등록하고 토큰 발급] 시 1회 표시됩니다.
#   Python 이 없으면 묶음의 windows\python-3.12.*-amd64.exe 를 조용히 설치하고,
#   cryptography 는 windows\wheels 에서 오프라인으로 설치합니다.
# ─────────────────────────────────────────────────────────────────────────────
param(
  [Parameter(Mandatory = $true)][string]$Platform,
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$Scan = "C:\certs",
  [string]$WinStore = "LocalMachine\My",
  [string]$CaFile = ""
)
$ErrorActionPreference = "Stop"
$D = Split-Path -Parent $MyInvocation.MyCommand.Path
function Die($m) { Write-Host "[중단] $m" -ForegroundColor Red; exit 1 }

# 1) Python 3.9+
$py = $null
foreach ($c in @("C:\Program Files\Python312\python.exe", "C:\Program Files\Python311\python.exe", "C:\Program Files\Python310\python.exe")) { if (Test-Path $c) { $py = $c; break } }
if (-not $py) {
  $inst = Get-ChildItem -Path $D, (Join-Path $D "windows"), (Join-Path $D "..\windows") -Filter "python-3.*-amd64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $inst) { Die "Python 이 없고 설치 파일(windows\python-3.12.x-amd64.exe)도 없습니다. 인터넷 PC에서 prepare_online.ps1 로 준비하세요." }
  Write-Host "Python 설치: $($inst.Name)"
  Start-Process $inst.FullName -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
  $py = "C:\Program Files\Python312\python.exe"
  if (-not (Test-Path $py)) { Die "Python 설치 확인 실패" }
}
# 2) cryptography (오프라인)
& $py -c "import cryptography" 2>$null
if ($LASTEXITCODE -ne 0) {
  $wh = @((Join-Path $D "windows\wheels"), (Join-Path $D "..\windows\wheels"), (Join-Path $D "wheels\win")) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $wh) { Die "cryptography 휠(windows\wheels)이 없습니다. 인터넷 PC에서 prepare_online.ps1 로 준비하세요." }
  & $py -m pip install --quiet --no-index --find-links $wh cryptography
  if ($LASTEXITCODE -ne 0) { Die "cryptography 오프라인 설치 실패" }
}
# 3) Agent 파일·설정(관리자·SYSTEM 만 읽기)
$ag = @((Join-Path $D "cert_agent.py"), (Join-Path $D "..\src\remote\cert_agent.py")) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $ag) { Die "cert_agent.py 를 찾지 못했습니다" }
New-Item -ItemType Directory -Force C:\cert-agent, C:\ProgramData\cert-agent | Out-Null
Copy-Item $ag C:\cert-agent\cert_agent.py -Force
$cfg = [ordered]@{ platform = $Platform.TrimEnd("/"); token = $Token; scan = @($Scan.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }); winstore = @($WinStore); service = @{} }
if ($CaFile) { $cfg.cafile = $CaFile }
$cfg | ConvertTo-Json | Set-Content C:\ProgramData\cert-agent\agent.json -Encoding UTF8
icacls C:\ProgramData\cert-agent /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
# 4) 작업 스케줄러: 30분마다 전체 수집, 1분마다 수집 요청·승인된 배포 확인
$a = New-ScheduledTaskAction -Execute $py -Argument "C:\cert-agent\cert_agent.py run"
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "cert-agent" -Action $a -Trigger $t -User SYSTEM -RunLevel Highest -Force | Out-Null
$a2 = New-ScheduledTaskAction -Execute $py -Argument "C:\cert-agent\cert_agent.py poll"
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "cert-agent-poll" -Action $a2 -Trigger $t2 -User SYSTEM -RunLevel Highest -Force | Out-Null
# 5) 첫 실행
& $py C:\cert-agent\cert_agent.py run
if ($LASTEXITCODE -ne 0) { Die "첫 실행 실패: 플랫폼 주소·토큰·방화벽·CaFile 을 확인하세요" }
Write-Host "설치 완료 — 플랫폼 [수집 승인] 탭에서 결과를 확인하세요. (작업 스케줄러: cert-agent, cert-agent-poll)" -ForegroundColor Green

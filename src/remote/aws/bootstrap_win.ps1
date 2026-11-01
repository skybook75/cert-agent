# AWS 테스트 서버 부트스트랩 (Windows Server 2022, IIS) — CloudFormation user-data 가 호출
$ErrorActionPreference = "Stop"
Start-Transcript C:\cert-lab-bootstrap.log -Append
New-Item -ItemType Directory -Force C:\cert-lab, C:\cert-agent | Out-Null
Install-WindowsFeature Web-Server -IncludeManagementTools | Out-Null

# Python + cryptography (Agent·테스트 인증서 생성용)
$py = "C:\cert-lab\python-installer.exe"
Invoke-WebRequest https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe -OutFile $py -UseBasicParsing
Start-Process $py -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
$env:Path = "C:\Program Files\Python312;C:\Program Files\Python312\Scripts;" + $env:Path
python -m pip install --quiet cryptography

# 소스 (git 없이 zip)
Invoke-WebRequest https://github.com/skybook75/cert-agent/archive/refs/heads/main.zip -OutFile C:\cert-lab\src.zip -UseBasicParsing
Expand-Archive C:\cert-lab\src.zip C:\cert-lab -Force
$P = "C:\cert-lab\cert-agent-main\src"
Copy-Item "$P\remote\cert_agent.py" C:\cert-agent\ -Force

# 테스트 인증서: portal.pfx 를 저장소에 넣고 IIS 443 에 바인딩, 만료된 intranet.cer 도 저장소에 추가
python "$P\remote\aws\lab_certs.py" win01 init
Import-Certificate -FilePath C:\cert-lab\ca.cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
$pw = ConvertTo-SecureString "Iis#Portal9" -AsPlainText -Force   # lab_certs.py 의 테스트 전용 값
$c = Import-PfxCertificate -FilePath C:\certs\portal.pfx -CertStoreLocation Cert:\LocalMachine\My -Password $pw
Import-Certificate -FilePath C:\certs\intranet.cer -CertStoreLocation Cert:\LocalMachine\My | Out-Null
Import-Module WebAdministration
New-WebBinding -Name "Default Web Site" -Protocol https -Port 443 -HostHeader "" -ErrorAction SilentlyContinue
(Get-WebBinding -Name "Default Web Site" -Protocol https).AddSslCertificate($c.Thumbprint, "My")
New-NetFirewallRule -DisplayName "HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow | Out-Null

# agent-setup.ps1 <플랫폼 URL> <토큰>
@'
param([string]$Platform, [string]$Token)
New-Item -ItemType Directory -Force C:\ProgramData\cert-agent | Out-Null
@{ platform = $Platform; token = $Token; scan = @("C:\certs"); winstore = @("LocalMachine\My"); service = @{} } | ConvertTo-Json | Set-Content C:\ProgramData\cert-agent\agent.json -Encoding UTF8
icacls C:\ProgramData\cert-agent /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
$a = New-ScheduledTaskAction -Execute "C:\Program Files\Python312\python.exe" -Argument "C:\cert-agent\cert_agent.py run"
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "cert-agent" -Action $a -Trigger $t -User SYSTEM -RunLevel Highest -Force | Out-Null
$a2 = New-ScheduledTaskAction -Execute "C:\Program Files\Python312\python.exe" -Argument "C:\cert-agent\cert_agent.py poll"
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "cert-agent-poll" -Action $a2 -Trigger $t2 -User SYSTEM -RunLevel Highest -Force | Out-Null
& "C:\Program Files\Python312\python.exe" C:\cert-agent\cert_agent.py run
'@ | Set-Content C:\cert-agent\agent-setup.ps1 -Encoding UTF8
"done" | Set-Content C:\cert-lab\bootstrap.done
Stop-Transcript

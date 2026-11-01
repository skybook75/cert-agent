#!/bin/bash
# 플랫폼 서버에 시나리오 러너(Playwright + Chromium + 한글 글꼴) 설치
set -euo pipefail
install -d -m 700 /var/lib/cert-lab-run
install -d /opt/lab-runner
cd /opt/lab-runner
command -v node >/dev/null || dnf install -y -q nodejs
dnf install -y -q nss atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite libXdamage libXrandr libXfixes mesa-libgbm pango cairo alsa-lib libXScrnSaver gtk3 >/dev/null 2>&1 || true
if ! fc-list 2>/dev/null | grep -qi "noto sans cjk\|nanum"; then
  dnf install -y -q google-noto-sans-cjk-ttc-fonts >/dev/null 2>&1 || dnf install -y -q google-noto-sans-cjk-fonts >/dev/null 2>&1 || {
    install -d /usr/share/fonts/noto-cjk
    curl -fsSL -o /usr/share/fonts/noto-cjk/NotoSansCJKkr-Regular.otf https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Korean/NotoSansCJKkr-Regular.otf
    curl -fsSL -o /usr/share/fonts/noto-cjk/NotoSansCJKkr-Bold.otf https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Korean/NotoSansCJKkr-Bold.otf
    fc-cache -f >/dev/null 2>&1 || true
  }
fi
[ -f package.json ] || npm init -y >/dev/null
[ -d node_modules/playwright ] || npm i -s playwright@1.48.2 >/dev/null 2>&1
npx playwright install chromium >/dev/null 2>&1
cp /opt/cert-agent-src/src/remote/aws/lab_runner.mjs /opt/lab-runner/
node -e "require('playwright').chromium.launch().then(b=>{console.log('chromium ok');return b.close()})"
fc-list | grep -ci "cjk\|nanum" | sed 's/^/korean fonts: /'

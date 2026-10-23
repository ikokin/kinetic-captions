#!/usr/bin/env bash
# Install the kinetic-captions bot on a server. Ubuntu/Debian, as root.
#   bash install.sh <TELEGRAM_TOKEN>
set -euo pipefail

TOKEN="${1:-}"
[ -z "$TOKEN" ] && { echo "usage: bash install.sh <TELEGRAM_TOKEN>"; exit 1; }

APP=/opt/kinetic
MODELS=$APP/models
say() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-venv git build-essential cmake \
                       fonts-dejavu-core fontconfig curl ca-certificates

say "checking ffmpeg for libass"
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -qE '^ [TSC.]+ +ass '; then
  echo "ERROR: this ffmpeg has no libass — it cannot draw subtitles." >&2
  echo "Install a build with libass, otherwise no text appears." >&2
  exit 1
fi
echo "libass present"

say "fonts"
# Inter covers Latin and Cyrillic; DejaVu is already installed as a fallback
mkdir -p /usr/local/share/fonts/kinetic
if [ ! -f /usr/local/share/fonts/kinetic/Inter.ttf ]; then
  curl -fsSL -o /tmp/inter.zip \
    "https://github.com/rsms/inter/releases/download/v4.0/Inter-4.0.zip" || true
  if [ -f /tmp/inter.zip ]; then
    python3 - <<'PY' || true
import zipfile, shutil, glob, os
z = zipfile.ZipFile('/tmp/inter.zip')
for n in z.namelist():
    if n.endswith('InterVariable.ttf'):
        with z.open(n) as s, open('/usr/local/share/fonts/kinetic/Inter.ttf','wb') as d:
            shutil.copyfileobj(s, d)
        break
PY
  fi
fi
fc-cache -f >/dev/null
FONT_NAME="$(fc-match 'Inter' family 2>/dev/null | head -1 | tr -d '\n' || echo 'DejaVu Sans')"
[ -z "$FONT_NAME" ] && FONT_NAME="DejaVu Sans"
echo "font: $FONT_NAME"

say "whisper.cpp"
if ! command -v whisper-cli >/dev/null 2>&1; then
  rm -rf /tmp/whisper.cpp
  git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp /tmp/whisper.cpp
  cmake -S /tmp/whisper.cpp -B /tmp/whisper.cpp/build -DCMAKE_BUILD_TYPE=Release >/dev/null
  cmake --build /tmp/whisper.cpp/build -j"$(nproc)" --target whisper-cli >/dev/null
  install -m755 /tmp/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
fi
whisper-cli --help >/dev/null 2>&1 && echo "whisper-cli ready"

say "speech model"
mkdir -p "$MODELS"
RAM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if [ "$RAM_MB" -ge 3500 ]; then
  MODEL_NAME="ggml-large-v3-turbo.bin"; echo "RAM ${RAM_MB}MB -> large-v3-turbo (best quality)"
else
  MODEL_NAME="ggml-small.bin";          echo "RAM ${RAM_MB}MB -> small (large will not fit)"
fi
if [ ! -f "$MODELS/$MODEL_NAME" ]; then
  curl -fL --progress-bar -o "$MODELS/$MODEL_NAME" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL_NAME"
fi

say "code"
mkdir -p "$APP"
install -m644 "$(dirname "$0")/../kinetic.py" "$APP/kinetic.py"
install -m644 "$(dirname "$0")/bot.py"        "$APP/bot.py"
mkdir -p "$APP/work"

cat > "$APP/.env" <<EOF
KIN_TOKEN=$TOKEN
WHISPER_MODEL=$MODELS/$MODEL_NAME
WHISPER_BIN=/usr/local/bin/whisper-cli
KIN_FONT=$FONT_NAME
KIN_LANG=ru
KIN_WORK=$APP/work
EOF
chmod 600 "$APP/.env"

say "systemd unit"
cat > /etc/systemd/system/kinetic-bot.service <<EOF
[Unit]
Description=Kinetic video bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=/usr/bin/python3 $APP/bot.py
Restart=always
RestartSec=5
# encoding is CPU-hungry; stay polite to the rest of the box
Nice=10
MemoryMax=3G

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now kinetic-bot
sleep 2
systemctl --no-pager --lines=15 status kinetic-bot || true

say "done"
echo "logs:     journalctl -u kinetic-bot -f"
echo "restart:  systemctl restart kinetic-bot"
echo "config:   $APP/.env"

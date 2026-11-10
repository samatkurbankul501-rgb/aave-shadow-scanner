#!/bin/bash
# setup.sh: развёртка shadow-сканера на Ubuntu 22.04/24.04/26.04
# Запускать от того же пользователя, под которым будет работать сервис
# (владелец /opt/aave-scanner и User= в unit совпадают).
set -e

SERVICE_USER="${SERVICE_USER:-$USER}"
if [ "$SERVICE_USER" != "$USER" ]; then
  echo "ERROR: запусти setup.sh от пользователя $SERVICE_USER (или не задавай SERVICE_USER)"
  exit 1
fi

echo "=== [1/7] System packages + Python >= 3.11 ==="
sudo apt-get update
sudo apt-get install -y git curl logrotate ufw
# На 26.04 пакета python3.11 нет — берём системный python3 (>= 3.11);
# на 22.04/24.04 ставим python3.11 явно.
if apt-cache show python3.11 >/dev/null 2>&1; then
  sudo apt-get install -y python3.11 python3.11-venv
  PY=python3.11
else
  sudo apt-get install -y python3 python3-venv
  PY=python3
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "ERROR: нужен Python >= 3.11, системный слишком стар"; exit 1; }
"$PY" --version

echo "=== [2/7] Clone / update ==="
REPO="${REPO_URL:-https://github.com/samatkurbankul501-rgb/aave-shadow-scanner.git}"
APP="/opt/aave-scanner"
sudo mkdir -p "$APP" && sudo chown "$USER:$USER" "$APP"
if [ -d "$APP/.git" ]; then
  cd "$APP" && git pull
else
  git clone "$REPO" "$APP"
fi
cd "$APP"

echo "=== [3/7] venv + deps ==="
"$PY" -m venv .venv
. .venv/bin/activate
pip install -q -U pip
pip install -q -r requirements.txt
python -c "import web3; print('web3 ok', web3.__version__)"

mkdir -p "$APP/logs"

echo "=== [4/7] Env check ==="
if [ ! -f "$APP/.env" ]; then
  echo "Создай $APP/.env с: POLYGON_RPC=https://..."
  echo "POLYGON_RPC=https://polygon-bor-mainnet.public.blastapi.io" > "$APP/.env"
fi
chmod 600 "$APP/.env"

echo "=== [5/7] systemd (User=$USER — владелец каталога) ==="
sed "s/^User=.*/User=$USER/" deploy/scanner.service | sudo tee /etc/systemd/system/aave-scanner.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable aave-scanner
# Не запускать сразу — сначала smoke вручную:
# sudo systemctl start aave-scanner
# journalctl -u aave-scanner -f

echo "=== [6/7] logrotate (14 дней — окно гейта B->C) ==="
sudo tee /etc/logrotate.d/aave-scanner >/dev/null <<'EOF'
/opt/aave-scanner/logs/*.jsonl /opt/aave-scanner/logs/*.json {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
# cursor.txt не ротацияется — паттерны только *.jsonl/*.json

echo "=== [7/7] ufw: правило SSH подготовлено, включение ВРУЧНУЮ ==="
sudo ufw allow 22/tcp
# НЕ включаем ufw автоматически: на shared-сервере default deny incoming
# оборвёт чужие inbound-сервисы. Включение — осознанное решение админа:
# sudo ufw enable
echo "ufw: allow 22/tcp добавлен; enable вручную (shared-сервер, чужие сервисы)"

echo "=== done ==="
echo "Smoke: $APP/.venv/bin/python -m scanner.scan --rpc \$(grep POLYGON_RPC $APP/.env | cut -d= -f2-) --from_block <cur-1_200_000> --out logs  # ~2 недели истории; 55M блоков = ~2.6 года"
echo "Start service: sudo systemctl start aave-scanner"
echo "Logs: journalctl -u aave-scanner -f"
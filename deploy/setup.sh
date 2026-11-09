#!/bin/bash
# setup.sh: развёртка shadow-сканера на Ubuntu 22.04/24.04
set -e

echo "=== [1/6] System packages ==="
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv git curl ufw
python3.11 --version

echo "=== [2/6] Clone / update ==="
REPO="${REPO_URL:-https://github.com/yourname/nn.git}"
APP="/opt/aave-scanner"
sudo mkdir -p "$APP" && sudo chown "$USER:$USER" "$APP"
if [ -d "$APP/.git" ]; then
  cd "$APP" && git pull
else
  rm -rf "$APP" && git clone "$REPO" "$APP"
fi
cd "$APP"

echo "=== [3/6] venv + deps ==="
python3.11 -m venv .venv
. .venv/bin/activate
pip install -q -U pip
pip install -q -r requirements.txt
python -c "import web3; print('web3 ok', web3.__version__)"

mkdir -p "$APP/logs"

echo "=== [4/6] Env check ==="
if [ ! -f "$APP/.env" ]; then
  echo "Создай $APP/.env с: POLYGON_RPC=https://..."
  echo "POLYGON_RPC=https://polygon-bor-mainnet.public.blastapi.io" > "$APP/.env"
fi
chmod 600 "$APP/.env"

echo "=== [5/6] systemd ==="
sudo cp deploy/scanner.service /etc/systemd/system/aave-scanner.service
sudo systemctl daemon-reload
sudo systemctl enable aave-scanner
# Не запускать сразу — сначала smoke вручную:
# sudo systemctl start aave-scanner
# journalctl -u aave-scanner -f

echo "=== [6/6] done ==="
echo "Smoke: $APP/.venv/bin/python -m scanner.scan --rpc \$(grep POLYGON_RPC $APP/.env | cut -d= -f2-) --from_block <cur-1_200_000> --out logs  # ~2 недели истории; 55M блоков = ~2.6 года"
echo "Start service: sudo systemctl start aave-scanner"
echo "Logs: journalctl -u aave-scanner -f"

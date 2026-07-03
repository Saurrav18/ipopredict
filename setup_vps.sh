#!/usr/bin/env bash
# =============================================================================
#  IPOPredict - one-time VPS setup (Ubuntu 22.04+, Oracle Always-Free ARM/x86)
#  Installs Python 3.12, Google Chrome (for the scraper), and all Python deps.
#  Run once:   bash setup_vps.sh
# =============================================================================
set -e
echo ">>> IPOPredict VPS setup starting..."

# --- system packages ---
sudo apt-get update -y
sudo apt-get install -y software-properties-common curl wget unzip gnupg

# --- Python 3.12 ---
if ! command -v python3.12 >/dev/null 2>&1; then
  echo ">>> installing Python 3.12..."
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -y
  sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
fi

# --- Google Chrome (for Selenium scraper) ---
# On ARM (Oracle Ampere) Chrome isn't available -> use Chromium instead.
ARCH="$(uname -m)"
if [ "$ARCH" = "x86_64" ]; then
  if ! command -v google-chrome >/dev/null 2>&1; then
    echo ">>> installing Google Chrome (x86_64)..."
    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get install -y /tmp/chrome.deb
  fi
else
  echo ">>> ARM detected -> installing Chromium..."
  sudo apt-get install -y chromium-browser chromium-chromedriver || sudo snap install chromium
fi

# --- Python virtual env + deps ---
echo ">>> creating virtual environment and installing Python packages..."
python3.12 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-server.txt 2>/dev/null || true
# scraper + ML deps (in case not in requirements files)
pip install selenium webdriver-manager pandas numpy openpyxl scikit-learn xgboost lightgbm yfinance uvicorn fastapi pydantic

echo ""
echo ">>> DONE. Next steps:"
echo "    1) copy your .env into this folder (with your secrets)"
echo "    2) start the site:       . .venv/bin/activate && uvicorn app:app --host 127.0.0.1 --port 8000"
echo "    3) set up the scheduler:  crontab -e   (see HOSTING_VPS.md)"

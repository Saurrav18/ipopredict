#!/bin/bash
# IPO Project, one-shot setup script for Linux/Mac
# Run: chmod +x setup.sh && ./setup.sh

set -e

echo "═══════════════════════════════════════════════════════"
echo "IPO PROJECT, Local Setup"
echo "═══════════════════════════════════════════════════════"

# 1. Create folder structure
echo ""
echo "[1/5] Creating folders..."
mkdir -p data output logs
echo "  ✅ data/ output/ logs/ created"

# 2. Move dataset if present
if [ -f "GMP_ML_READY_FINAL_v3.xlsx" ]; then
    mv GMP_ML_READY_FINAL_v3.xlsx data/
    echo "  ✅ Dataset moved to data/"
fi

# 3. Install Python deps
echo ""
echo "[2/5] Installing Python packages..."
pip install -q -r requirements.txt
echo "  ✅ Dependencies installed"

# 4. Set env var for dataset location
export IPO_DATASET="$PWD/data/GMP_ML_READY_FINAL_v3.xlsx"
export IPO_DATA_DIR="$PWD/output"
echo "export IPO_DATASET=\"$IPO_DATASET\"" >> ~/.bashrc
echo "export IPO_DATA_DIR=\"$IPO_DATA_DIR\"" >> ~/.bashrc
echo "  ✅ Env vars saved to ~/.bashrc"

# 5. First-time runs
echo ""
echo "[3/5] Running first scrape + score (this takes 1-2 min)..."
python3 ipo_runner.py --now || echo "  ⚠️  Runner had issues, check logs"

echo ""
echo "[4/5] Calibrating slider (one-time, takes ~30 sec)..."
python3 ipo_update.py --force || echo "  ⚠️  Update failed, check error above"

echo ""
echo "[5/5] Computing per-IPO tiers..."
python3 ipo_qualifier.py --demo || echo "  ⚠️  Qualifier failed"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "✅ SETUP COMPLETE"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. Start the backend:  uvicorn app:app --reload --port 8000"
echo "  2. Open browser:       http://localhost:8000/docs"
echo "  3. Add cron jobs:      see DEPLOY_GUIDE.md"
echo ""

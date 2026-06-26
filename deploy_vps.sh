#!/bin/bash
# Deploy to predict.hetaisheng.ccwu.cc VPS
# Run this on the VPS (170.106.198.250) after pushing new models

set -e
VPS_USER="ubuntu"
VPS_HOST="170.106.198.250"
VPS_PATH="~/soccerdata"
LOCAL_PATH="/home/hetaisheng/soccerdata"

echo "=== Step 1: Push latest code to VPS ==="
rsync -avz --exclude '.venv/' --exclude '__pycache__/' \
    --exclude '.git/' --exclude 'data/FBref/' --exclude 'data/Sofascore/' \
    --exclude 'logs/' --exclude '.omx/' --exclude '.claude/' \
    --exclude '.slim/deepwork/' \
    -e ssh "$LOCAL_PATH/" "$VPS_USER@$VPS_HOST:$VPS_PATH/"

echo ""
echo "=== Step 2: Push latest data files ==="
ssh "$VPS_USER@$VPS_HOST" "mkdir -p ~/soccerdata/data ~/soccerdata/wc_analysis/data"
scp "$LOCAL_PATH/data/international_results.csv" "$VPS_USER@$VPS_HOST:$VPS_PATH/data/"
scp "$LOCAL_PATH/data/worldcup_history/data-csv/matches.csv" "$VPS_USER@$VPS_HOST:$VPS_PATH/data/worldcup_history/data-csv/" || true
scp "$LOCAL_PATH/wc_analysis/data/"*.pkl "$VPS_USER@$VPS_HOST:$VPS_PATH/wc_analysis/data/" || true
scp "$LOCAL_PATH/wc_analysis/data/"*.json "$VPS_USER@$VPS_HOST:$VPS_PATH/wc_analysis/data/" || true
scp "$LOCAL_PATH/wc_analysis/data/top3_predictions.json" "$VPS_USER@$VPS_HOST:$VPS_PATH/wc_analysis/data/" || true

echo ""
echo "=== Step 3: Restart predict.py --serve on VPS ==="
ssh "$VPS_USER@$VPS_HOST" << 'EOF'
cd ~/soccerdata
# Kill old process
pkill -f "predict.py --serve" || true
sleep 2
# Start new process
source .venv/bin/activate 2>/dev/null || python3 -m venv .venv && source .venv/bin/activate
pip install -q -r requirements.txt 2>/dev/null
nohup python wc_analysis/predict.py --serve > ~/soccerdata/logs/serve.log 2>&1 &
echo "Started PID $!"
sleep 3
# Verify
curl -sI http://localhost:8026/ | head -3
EOF

echo ""
echo "=== Step 4: Trigger refresh + verify ==="
sleep 2
curl -s -X POST "https://predict.hetaisheng.ccwu.cc/api/refresh" | head -5
echo ""
echo "=== Deploy complete ==="

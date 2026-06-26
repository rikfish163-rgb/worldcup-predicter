#!/bin/bash
# relay_sporttery.sh
# Runs the sporttery scrape on 4090 (which has China IP), then pushes the result to VPS.
# Schedule via cron: */30 * * * * /home/hetaisheng/soccerdata/wc_analysis/relay_sporttery.sh

set -e

LOG="/home/hetaisheng/soccerdata/wc_analysis/data/relay.log"
TS() { date "+%Y-%m-%d %H:%M:%S"; }

echo "[$(TS)] relay start" >> "$LOG"

# Step 1: Trigger scrape on 4090
SCRAPE_OUT=$(ssh -p 41380 -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
    hts@110os9214fc69.vicp.fun \
    "cd ~/soccerdata/wc_analysis && python3 scrape_sporttery.py 2>&1" 2>&1)
echo "[$(TS)] scrape: $SCRAPE_OUT" >> "$LOG"

# Step 2: Pull file from 4090 to local
LOCAL_DIR="/home/hetaisheng/soccerdata/wc_analysis/data"
scp -P 41380 -o ConnectTimeout=10 \
    hts@110os9214fc69.vicp.fun:~/soccerdata/wc_analysis/data/odds_parsed.json \
    "$LOCAL_DIR/odds_parsed_4090.json" >> "$LOG" 2>&1
echo "[$(TS)] pulled odds_parsed.json from 4090" >> "$LOG"

# Step 3: Push to VPS
scp -o ConnectTimeout=10 \
    "$LOCAL_DIR/odds_parsed_4090.json" \
    ubuntu@170.106.198.250:~/soccerdata/wc_analysis/data/odds_parsed.json >> "$LOG" 2>&1
scp -o ConnectTimeout=10 \
    "$LOCAL_DIR/odds_parsed_4090.json" \
    ubuntu@170.106.198.250:~/soccerdata/wc_analysis/data/odds_parsed_fresh.json >> "$LOG" 2>&1
echo "[$(TS)] pushed to VPS" >> "$LOG"

echo "[$(TS)] relay done" >> "$LOG"

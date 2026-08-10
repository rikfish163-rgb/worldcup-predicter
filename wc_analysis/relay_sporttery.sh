#!/bin/bash
# relay_sporttery.sh
# Runs the sporttery scrape on 4090 (which has China IP), then pushes the result to VPS.
# Schedule via cron: */2 * * * * /home/hetaisheng/soccerdata/wc_analysis/relay_sporttery.sh
# (注释频率此前写"*/30分钟", 与实际crontab(每2分钟)不一致15倍, 已修正为实际值)
#
# 双链路说明: 本机还有一条push_odds.sh(每8分钟直连抓体彩), 与这条4090中转
# 链路(每2分钟)并行写VPS上同名的odds_parsed.json。两条链路都用rsync -au
# (仅源比目标新才覆盖)做版本保护, 避免旧数据反向打回新数据; 保留双链路是
# 有意为之(直连更权威, 4090中转频率更高作为补充), 不做单链路合并。

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
# (审计修复2026-07-02: 此前用scp无条件覆盖odds_parsed.json——这个文件名
# push_odds.sh(本机直连链路)也在写, 两条链路无锁无版本号并行写同一个文件,
# 谁最后跑谁生效, 可能用这次4090中转抓到的旧数据覆盖刚才直连抓到的新数据。
# 改用rsync -au(仅当本地文件比VPS上的新才覆盖), 跟push_odds.sh统一到同一套
# 保护机制, 而不是新增一把独立的锁。odds_parsed_fresh.json只有这条链路写,
# 不存在双写风险, 但同样统一用rsync -au以防未来又多一条写它的链路。)
rsync -au -e "ssh -o ConnectTimeout=10" \
    "$LOCAL_DIR/odds_parsed_4090.json" \
    ubuntu@170.106.198.250:~/soccerdata/wc_analysis/data/odds_parsed.json >> "$LOG" 2>&1
rsync -au -e "ssh -o ConnectTimeout=10" \
    "$LOCAL_DIR/odds_parsed_4090.json" \
    ubuntu@170.106.198.250:~/soccerdata/wc_analysis/data/odds_parsed_fresh.json >> "$LOG" 2>&1
echo "[$(TS)] pushed to VPS(rsync -u, 仅推送更新过的)" >> "$LOG"

echo "[$(TS)] relay done" >> "$LOG"

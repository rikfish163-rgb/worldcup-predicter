#!/bin/bash
# 每日自动抓取+分析 (crontab 用)
# 建议 crontab 条目:
#   0 8,14,20 * * * /home/hetaisheng/soccerdata/wc_analysis/daily_update.sh >> /home/hetaisheng/soccerdata/wc_analysis/data/cron.log 2>&1
#
# 每天 8:00/14:00/20:00 自动刷新(体彩赔率每天变化多次,临场最准)

cd "$(dirname "$0")/.."
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 自动刷新 ==="
.venv/bin/python wc_analysis/predict.py
echo "完成"
echo ""

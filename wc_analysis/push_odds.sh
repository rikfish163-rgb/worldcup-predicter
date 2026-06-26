#!/bin/bash
# 本机定时推送: 抓体彩最新盘口 -> 推送VPS -> 触发VPS重算 -> 胜率随盘口动态更新
# 由 crontab 每 8 分钟调用 (本机是唯一能抓中国体彩的机器)
# 日志: wc_analysis/data/push.log
cd /home/hetaisheng/soccerdata

TS() { date '+%Y-%m-%d %H:%M:%S'; }

# 1. 抓体彩最新盘口(中国IP直连),更新 odds_parsed.json
.venv/bin/python -c "
import sys; sys.path.insert(0, 'wc_analysis')
from predict import fetch_sporttery
m = fetch_sporttery()
print(f'抓到 {len(m)} 场')
" 2>&1 | sed "s/^/[$(TS)] /"

# 2. 推送最新盘口到 VPS
if scp -i ~/.ssh/id_rsa -o BatchMode=yes -o ConnectTimeout=15 \
     wc_analysis/data/odds_parsed.json \
     ubuntu@170.106.198.250:~/soccerdata/wc_analysis/data/odds_parsed.json 2>/dev/null; then
  echo "[$(TS)] ✓ 盘口已推送VPS"
else
  echo "[$(TS)] ✗ 推送失败(VPS不可达或免密未配)"
  exit 1
fi

# 3. 触发VPS立即重算(读到新盘口->重新计算胜率->刷新页面)
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 \
       "https://predict.hetaisheng.ccwu.cc/api/refresh")
echo "[$(TS)] VPS重算: $CODE"

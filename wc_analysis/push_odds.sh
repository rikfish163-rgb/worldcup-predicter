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

# 2. 推送所有权威判定文件到 VPS。
# (修复2026-07-02: 此前只同步 odds_parsed.json + params_override.json 两个文件,
#  standings.json/wc_results.json/cohesion.json/corners.json/xg_profiles.json 等
#  从未同步过, VPS 一直用部署当天的静态快照推理 —— 这是"fighting_top2"这类
#  已过期小组赛标签能穿透到淘汰赛面板的根因(_is_knockout_stage()靠wc_results.json
#  场次数判定阶段, VPS那份是旧的66场, 本机已是72场, 判定就错了)。
#  用 rsync -u(仅当源比目标新才覆盖) 而非无条件scp, 防止本机偶发的旧缓存
#  反向覆盖VPS上可能更新的数据。
#
#  二次修复2026-07-02(全方位审查发现): params_override.json 已从这里移除——
#  它的真正生成者是 evolve_groupstage.py, 而该脚本只在 VPS 的 _auto_refresh_loop
#  里被调用, 本机从未运行过。之前把它列在本机→VPS方向纯属方向反了, 只是刚好
#  靠"本机文件mtime恰好比VPS旧"这个巧合没出错——git checkout/pull会重置
#  mtime, 一旦巧合被打破, 就会用本机的旧值覆盖VPS自己刚进化出的新参数。
#  本机若需要只读查看当前生效的参数, 手动 scp 从VPS拉一次即可, 不需要常驻
#  的反向同步脚本。)
SYNC_FILES=(
  odds_parsed.json
  standings.json wc_results.json groups_2026.json
  cohesion.json corners.json injuries.json xg_profiles.json
  draw_model.json sofascore_features.json weather.json
)
EXISTING=()
for f in "${SYNC_FILES[@]}"; do
  [ -f "wc_analysis/data/$f" ] && EXISTING+=("wc_analysis/data/$f")
done
if rsync -au -e "ssh -i ~/.ssh/id_rsa -o BatchMode=yes -o ConnectTimeout=15" \
     "${EXISTING[@]}" \
     ubuntu@170.106.198.250:~/soccerdata/wc_analysis/data/ 2>/dev/null; then
  echo "[$(TS)] ✓ ${#EXISTING[@]}个数据文件已同步VPS(rsync -u, 仅推送更新过的)"
else
  echo "[$(TS)] ✗ 推送失败(VPS不可达或免密未配)"
  exit 1
fi

# 3. 触发VPS立即重算(读到新盘口->重新计算胜率->刷新页面)
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 \
       "https://predict.hetaisheng.ccwu.cc/api/refresh")
echo "[$(TS)] VPS重算: $CODE"

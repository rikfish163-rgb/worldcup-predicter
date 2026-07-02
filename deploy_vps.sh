#!/bin/bash
# Deploy to predict.hetaisheng.ccwu.cc VPS
# Run this on the VPS (170.106.198.250) after pushing new models

set -e
VPS_USER="ubuntu"
VPS_HOST="170.106.198.250"
VPS_PATH="~/soccerdata"
LOCAL_PATH="/home/hetaisheng/soccerdata"

# (审计修复2026-07-02: 此前Step1用rsync -avz(无-u)同步整个代码库, 没排除
#  wc_analysis/data/, 会无条件覆盖VPS的整个数据目录; Step2又对*.json做一次
#  scp(同样无条件, 用||true吞掉失败)——两步互相重叠, 且都没有push_odds.sh
#  已经在用的rsync -au"仅推送本机更新过的文件"保护。已用真实文件实测验证过
#  这不是空谈: 若跑这个脚本会把evolve_groupstage刚进化出的params_override.json
#  新参数、record_snapshot持续累积的odds_trend.json趋势历史、VPS长期运行
#  积累的prediction_history.json/predictions.json全部打回本机的旧版本或
#  直接清空。这几个文件的权威源是VPS(只在VPS的--serve长驻进程里持续写入),
#  不该被本机部署脚本覆盖, 必须整体排除在同步范围之外。)
VPS_AUTHORITATIVE_JSON=(
  params_override.json predictions.json prediction_history.json
  odds_trend.json news_factors.json
)
RSYNC_EXCLUDES=(--exclude '.venv/' --exclude '__pycache__/'
  --exclude '.git/' --exclude 'data/FBref/' --exclude 'data/Sofascore/'
  --exclude 'logs/' --exclude '.omx/' --exclude '.claude/'
  --exclude '.slim/deepwork/' --exclude 'wc_analysis/data/')

echo "=== Step 1: Push latest code to VPS (不含 wc_analysis/data/, 数据同步交给Step2统一处理) ==="
rsync -avz "${RSYNC_EXCLUDES[@]}" \
    -e ssh "$LOCAL_PATH/" "$VPS_USER@$VPS_HOST:$VPS_PATH/"

echo ""
echo "=== Step 2: Push latest data files (rsync -au, 排除VPS权威文件) ==="
ssh "$VPS_USER@$VPS_HOST" "mkdir -p ~/soccerdata/data ~/soccerdata/wc_analysis/data"
scp "$LOCAL_PATH/data/international_results.csv" "$VPS_USER@$VPS_HOST:$VPS_PATH/data/"
scp "$LOCAL_PATH/data/worldcup_history/data-csv/matches.csv" "$VPS_USER@$VPS_HOST:$VPS_PATH/data/worldcup_history/data-csv/" || true
scp "$LOCAL_PATH/wc_analysis/data/"*.pkl "$VPS_USER@$VPS_HOST:$VPS_PATH/wc_analysis/data/" || true

JSON_EXCLUDE_ARGS=()
for f in "${VPS_AUTHORITATIVE_JSON[@]}"; do
  JSON_EXCLUDE_ARGS+=(--exclude "$f")
done
rsync -au "${JSON_EXCLUDE_ARGS[@]}" -e ssh \
  "$LOCAL_PATH/wc_analysis/data/"*.json \
  "$VPS_USER@$VPS_HOST:$VPS_PATH/wc_analysis/data/"

echo ""
echo "=== Step 3: Restart wc-predict systemd service on VPS ==="
# (审计修复: 此前用pkill -f + nohup裸启动, 完全没对接现在的systemd管理
#  (wc-predict.service, Restart=always常驻)。裸启动会导致两个进程同时抢
#  8026端口, 且systemd检测到端口冲突/进程异常还会按自己的重启策略继续
#  介入, 造成混乱。改为直接调用systemctl, 与线上实际运维方式一致。)
ssh "$VPS_USER@$VPS_HOST" "sudo systemctl restart wc-predict && sleep 3 && systemctl is-active wc-predict"

echo ""
echo "=== Step 4: Trigger refresh + verify ==="
sleep 2
curl -s -X POST "https://predict.hetaisheng.ccwu.cc/api/refresh" | head -5
echo ""
echo "=== Deploy complete ==="

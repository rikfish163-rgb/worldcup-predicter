#!/bin/bash
# 自动更新预测页面并部署到 CF Pages
# 用法: ./wc_analysis/deploy.sh
# crontab: 0 6,12,18 * * * /home/hetaisheng/soccerdata/wc_analysis/deploy.sh >> /home/hetaisheng/soccerdata/wc_analysis/data/deploy.log 2>&1
#
# 注意: Cloudflare API token 必须从环境变量获取, 不要提交到 git.
# 在 ~/.bashrc 里 export CLOUDFLARE_API_TOKEN=... 和 CLOUDFLARE_ACCOUNT_ID=...

set -e
cd /home/hetaisheng/soccerdata

# 从环境变量读取 (不要硬编码)
: "${CLOUDFLARE_API_TOKEN:?请先 export CLOUDFLARE_API_TOKEN=...}"
: "${CLOUDFLARE_ACCOUNT_ID:?请先 export CLOUDFLARE_ACCOUNT_ID=...}"
export CLOUDFLARE_API_TOKEN
export CLOUDFLARE_ACCOUNT_ID

echo "$(date '+%Y-%m-%d %H:%M:%S') === 开始更新 ==="

# 1. 运行预测引擎生成最新 HTML
.venv/bin/python wc_analysis/predict.py
echo "predict.py 完成"

# 2. 复制到 personal-site
cp wc_analysis/site/index.html /home/hetaisheng/personal-site/predict/index.html
cp wc_analysis/site/messi.png /home/hetaisheng/personal-site/predict/messi.png
echo "文件已复制"

# 3. 部署到 CF Pages
npx wrangler@latest pages deploy /home/hetaisheng/personal-site \
  --project-name=htsweb \
  --branch=master \
  --commit-dirty=true
echo "$(date '+%Y-%m-%d %H:%M:%S') === 部署完成 ==="

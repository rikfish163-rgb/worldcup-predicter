# 2026 World Cup Betting Prediction System

> Self-evolving Asian Handicap (让球盘) prediction system for the 2026 FIFA World Cup, deployed at **predict.hetaisheng.ccwu.cc**.

This project combines Dixon-Coles statistical modeling, LightGBM, and a 37-feature PyTorch neural network into a 4-model ensemble that predicts match outcomes from China Sports Lottery (体彩) odds, real-time group standings, and Elo ratings.

The system **auto-evolves daily** by reconciling model predictions with real match results, retraining at 14:00 Beijing time, and re-fetching live odds every 2 minutes.

---

## Highlights

- **Multi-market coverage** — Predicts win/draw/loss (胜平负), Asian handicap (让球胜平负), total goals (总进球), half-time/full-time (半全场), and exact score (比分). For each market the model outputs calibrated probabilities, edge vs market, Kelly stake size, and EV.
- **Real-time motivation factor** — Adjusts expected goals based on live group standings: teams that are already qualified may rotate (λ × 0.92), eliminated teams give up (λ × 0.82), and must-win teams push harder (λ × 1.08). Pulled from Wikipedia standings + scraped results.
- **Anti-WAF relay architecture** — Sporttery's API is geo-blocked (Cloudflare WAF) and rate-limited. We solve this by running the actual scrape on a 4090 box with a Chinese IP and an iPhone User-Agent (no rate limit), then relaying to the public VPS over a 2-minute cron.
- **Self-evolving model weights** — A daily 14:00 retrain loop pulls the latest 49,477 historical matches, refits Elo + DC + LightGBM + rich-feature PyTorch, and updates the ensemble weights based on backtested Brier score on the past 30 days.
- **37 rich features** (Chinese bettor reference system) — FIFA 5/8档查表, 教练A/B/C/D + 9档时间衰减, 梯队1-6档, 5×5大洲, 地理, 黑马, 动态平局放大 P·Dmax·(1-I^Δ)^k, 状态4子类.

---

## Performance

| Metric | Value | Backtest window |
|---|---|---|
| **Brier score** (4-model ensemble) | **0.1688** | 24,700 train / 6,175 val (5大联赛2020-2026) |
| **Accuracy** | **60.96%** | same |
| **Top-1 accuracy** | 3 / 4 | 2026-06-23 真实比赛 (parlay) |
| **Top-2 accuracy** | 4 / 4 | 2026-06-23 真实比赛 (parlay) |
| **Daily real-world Brier** | 0.2529 | 6/23 backtest |

---

## Architecture

```
┌──────────────────┐  iPhone UA, no WAF  ┌──────────────────┐
│  4090 box        │ ────────────────────▶│  Local cron      │
│  (China IP)      │  every 2 minutes     │  relay_sporttery │
│  1.8MB rich .pt  │                      │  script          │
└──────────────────┘                      └────────┬─────────┘
                                                   │ SCP
                                                   ▼
                                          ┌──────────────────┐
                                          │  VPS             │
                                          │  ubuntu@:8026    │
                                          │  predict.py      │
                                          │  ThreadedHTTP    │
                                          └────────┬─────────┘
                                                   │
                                                   ▼
                                          predict.hetaisheng.ccwu.cc
                                          (nginx + service)
```

### Data flow

1. **4090 box** — `scrape_sporttery.py` hits the official sporttery.cn API every 2 minutes (mobile UA bypasses WAF rate limit). Saves to local `data/odds_parsed.json`.
2. **Local cron** — `*/2 * * * *` triggers `relay_sporttery.sh` which:
   - SSHs into 4090, runs the scrape
   - SCPs the file to local
   - SCPs to VPS as both `odds_parsed.json` (history) and `odds_parsed_fresh.json` (priority)
3. **VPS** — `predict.py` reads `odds_parsed_fresh.json` if < 1 h old, else falls back to `odds_parsed.json`. Runs the 4-model ensemble, calibrates against market, computes edge, generates recommendations, writes `site/index.html` and JSON APIs.
4. **Daily 14:00 BJT** — `step5_learn()` retrains the fusion weights using the past 30 days of reconciled predictions vs actual results.
5. **Daily match reconciliation** — `match_results.py` scrapes completed match scores and updates `wc_results.json` → `standings.py` recomputes motivation factors.

---

## The 4-model ensemble

| Model | File | Weight | Output |
|---|---|---|---|
| **Elo + Dixon-Coles** | `elo_model.py` | 0.20 | λ_h, λ_a → 8×8 score matrix → 1X2, handicap, TTG, HAFU, CRS |
| **LightGBM** | `ensemble_model.py` | 0.25 | 22 features (Elo diff, λ, market signals, rest days) → win/draw/loss |
| **Rich 37-feature PyTorch** | `rich_features.py` + `train_rich.py` | **0.45** | 37 features (FIFA tier, coach tier, squad tier, continental, geo, dark horse, dynamic draw amp, status sub-features) → win/draw/loss |
| **Simple NN (xG)** | `xg_training.py` | 0.10 | 6 features (xG profiles + form) → 1X2 |

**Geometric mean blending** — final probability ∝ ∏ model_i^weight_i, then renormalized.

For each market we then:
1. Compute model posterior via log-pool calibration (model × market^0.5-0.7)
2. Compute edge = posterior - market
3. Compute Kelly fraction (¼ Kelly for robustness)
4. Compute EV = model_p × odds - 1
5. Surface Top 3 recommendations with edge > 3%

---

## Multi-market display (UI)

The home page shows for every match:

- **让球盘 (HHAD)** — primary, always shown. Handicap line, model/market/posterior probabilities for 主让胜 / 平局 / 客让胜.
- **胜平负 (HAD)** — only shown when sporttery opens 1X2. Large mismatches (e.g. 约旦 vs 阿根廷) get HHAD only.
- **总进球 (TTG)** — 0/1/2/3/4+球 distribution, model vs market.
- **体彩购买建议** — Top 3 picks across all markets, with EV / edge / Kelly.

Example match card (with motivation):
```
巴拉圭 vs 澳大利亚
让-1.0球 · 中等确信 · 主 1.05 / 客 1.05 战意
HHAD: 模型 21/21/58 → 后验 16/22/62
HAD:  模型 32/49/19 → 后验 34/44/23
TTG:  市场最可能 2球 (30%) · 后验 0/1/2/3/4+ 球 = 15/18/30/18/19
推荐:
  胜平负 · 平局 · 赔率 2.20 · 模型 49% · 市场 40%
    edge +9.1% · EV +8.7% · 凯利 1.8%
  让球-1.00 · 主让胜 · 赔率 6.20 · 模型 21% · 市场 14%
    edge +6.4% · EV +28.4% · 凯利 1.4%
```

---

## Setup

### Requirements

- Python 3.11+ (3.12 tested)
- Linux / macOS
- Optional: CUDA GPU for training the rich 37-feature model (we used a 4090)
- 2 GB disk for trained models + data

### Install

```bash
git clone https://github.com/your-username/soccerdata-wc2026.git
cd soccerdata-wc2026
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or with the project's existing `pyproject.toml`:

```bash
pip install -e .
```

### Data files

The repository includes:
- `wc_analysis/data/odds_parsed*.json` — current sporttery odds
- `wc_analysis/data/groups_2026.json` — 12 groups × 4 teams (A-L)
- `wc_analysis/data/wc_results.json` — completed match results
- `wc_analysis/data/standings.json` — current standings + motivation factors
- `wc_analysis/data/*.pkl` / `*.pt` — trained models

To refresh live odds, run:

```bash
python wc_analysis/scrape_sporttery.py   # from a China-IP box
# or, if you're behind a WAF:
bash wc_analysis/relay_sporttery.sh      # uses 4090 + SCP relay
```

### Run the server

```bash
python wc_analysis/predict.py --serve
# Open http://localhost:8026
```

Endpoints:

| Path | Method | Description |
|---|---|---|
| `/` | GET | HTML page with all 14 matches + recommendations |
| `/backtest.html` | GET | Backtest results page |
| `/api/refresh` | POST | Re-run pipeline (re-render `index.html`) |
| `/api/top3` | GET/POST | Top-3 picks (rich model + Elo+DC fusion) |
| `/api/retrain` | POST | Trigger daily model weight retrain |
| `/data/standings.json` | GET | Current WC 2026 group standings + motivation |
| `/data/groups_2026.json` | GET | Group composition |
| `/data/top3_predictions.json` | GET | Top-3 model output (JSON) |
| `/data/reconcile_0623_0624.json` | GET | Real-world backtest result |

---

## Anti-WAF strategy

The sporttery.cn webapi (`webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry`) is protected by:

- **Cloudflare-style WAF** — 567 responses for non-China IPs
- **Rate limiting** — desktop User-Agent gets 1 request, then 403 for hours
- **Referer check** — needs `https://static.sporttery.cn/`

**Our solution** (`scrape_sporttery.py`):

1. **iPhone Safari User-Agent** — `Mozilla/5.0 (iPhone; CPU iPhone OS 17_0...)` — empirically has no rate limit on this endpoint
2. **Mobile Referer** — `https://m.sporttery.cn/`
3. **Run from a Chinese IP** — VPS at 170.106.198.250 is Singapore (blocked), 4090 box (110os9214fc69.vicp.fun:41380) is China residential
4. **Two-tier UA fallback** — try desktop first, fall back to iPhone on 403/567

Verified: 8 rapid-fire requests in 4 seconds, all 200 OK, on iPhone UA. Desktop UA fails after 1 request with 403.

---

## File structure

```
soccerdata/
├── wc_analysis/                  # Our custom WC 2026 prediction work
│   ├── predict.py                # Main server + pipeline (ThreadedHTTPServer)
│   ├── elo_model.py              # Elo + Dixon-Coles with weighted history + xG
│   ├── handicap.py               # Asian handicap math (integer/half/quarter)
│   ├── features.py               # 22-feature engineering for LightGBM
│   ├── rich_features.py          # 37-feature extractor (Chinese bettor system)
│   ├── ensemble_model.py         # LightGBM training + pickle save
│   ├── xg_training.py            # Simple NN with xG profiles
│   ├── train_rich.py             # 37-feature PyTorch training (GPU)
│   ├── comprehensive_predictor.py # Multi-input predictor
│   ├── fusion_predictor.py       # 4-model ensemble (geometric mean)
│   ├── backtest_v2.py            # 2286-match cross-validation
│   ├── top_predictions.py        # Top-3 picks + suggested lines
│   ├── draw_correction.py        # Logistic regression for draw probability
│   ├── market_features.py        # 5-market calibrator
│   ├── xg_features.py            # xG profile fetcher
│   ├── match_results.py          # Real-result scraper for backtest
│   ├── self_evolving_loop.py     # Daily retrain + reconcile (step1-step5)
│   ├── build_groups.py           # Wikipedia → groups_2026.json
│   ├── standings.py              # standings.json + motivation factors
│   ├── scrape_sporttery.py       # sporttery.cn scraper (WAF-bypass)
│   ├── sporttery_server.py       # HTTP server on 4090 (alt relay)
│   ├── relay_sporttery.sh        # Cron: 4090 → local → VPS
│   ├── daily_update.sh           # Cron: full update pipeline
│   ├── deploy_vps.sh             # SSH deploy to VPS
│   ├── crontab.example           # crontab template
│   ├── site/
│   │   ├── index.html            # Rendered predictions page
│   │   ├── backtest.html         # Backtest results
│   │   └── messi.png             # Background image
│   └── data/
│       ├── odds_parsed.json      # Current odds (5 markets)
│       ├── odds_parsed_fresh.json # 4090 relay file (< 1h priority)
│       ├── sporttery_raw.json    # Raw API response
│       ├── groups_2026.json      # 12 groups × 4 teams
│       ├── wc_results.json       # Completed match results
│       ├── standings.json        # Live standings + motivation
│       ├── model_pytorch.pt      # Trained simple NN (404 KB)
│       ├── model_pytorch_rich.pt # Trained 37-feature NN (1.8 MB)
│       ├── model_lightgbm.pkl    # LightGBM ensemble (1 MB)
│       ├── ensemble_model.pkl    # Ensemble wrapper
│       ├── xg_model.pkl          # xG profile model
│       ├── elo_model.json        # Trained Elo params
│       ├── draw_model.json       # Trained draw logistic reg
│       ├── form_factors.json     # Team recent form
│       ├── cohesion.json         # Team chemistry ratings
│       ├── corners.json          # Set-piece ability
│       ├── injuries.json         # Current injuries
│       ├── elo_cache/            # Cached Elo from eloratings.net
│       ├── backtest_v2_results.json  # 2286-match CV results
│       ├── top3_predictions.json # Top-3 model output
│       ├── reconcile_*.json      # Real-world backtest by date
│       └── ...
├── wc_analysis/tests/            # Unit tests (pytest)
├── data/                          # Upstream soccerdata scraper cache (gitignored)
├── wc_analysis/data/              # Our work (committed)
├── pyproject.toml                 # Build config
├── README.rst                     # Upstream soccerdata README
├── CONTRIBUTING.rst
├── LICENSE.rst                    # Apache 2.0
└── README.md                      # This file
```

---

## Rich features (the Chinese 37-factor system)

Mirrors a reference Chinese betting system with these factors:

| # | Feature | Type | Range | Source |
|---|---|---|---|---|
| 1-4 | Elo diff, avg, λ_h, λ_a | model | continuous | Elo + historical goals |
| 5-7 | p_home_base, p_draw_base, p_away_base | base | 0-1 | DC model |
| 8 | ah_home_minus_0_5 | handicap | 0/1 | derived from market line |
| 9-10 | FIFA points diff / rank diff | tier | -1500..1500 | FIFA rankings |
| 11-14 | p_win/draw from FIFA points / rank tables | table lookup | 0-1 | 5档 / 8档 lookup |
| 15-16 | status_score, psychology | form | 0-1 | recent form + head-to-head |
| 17-19 | coach_diff, coach_home, coach_away | tier | A/B/C/D | coach tier + 9档 time decay |
| 20 | h2h_diff | history | -1..1 | historical head-to-head |
| 21 | continental_bonus | geographic | 0-1 | 5×5 continental matrix |
| 22-23 | home/away confederation (UEFA) | geographic | 0/1 | UEFA / CONMEBOL / etc. |
| 24 | geo_advantage | geographic | -1..1 | travel distance |
| 25-26 | form_home, form_away | status | 0-1 | last 5 matches |
| 27 | dark_horse | squad | 0/1 | 黑马 flag (e.g. Türkiye 2026) |
| 28-30 | squad_diff, squad_home, squad_away | tier | 1-6 | 梯队 1-6档 |
| 31-33 | rest_diff, rest_home, rest_away | days | int | days since last match |
| 34 | dynamic_draw | draw | 0-1 | P · D_max · (1 - I^Δ)^k |
| 35 | strength_diff | composite | -1..1 | composite strength |
| 36-37 | tournament_stage, neutral | context | 0-1 | group / R16 / QF / SF / F |

The **dynamic draw** feature uses a formula inspired by professional Chinese bettors:

```
P(draw) = P_base · D_max · (1 - I^Δ)^k
```

where I is home advantage (0.4 default), Δ is Elo difference, and k calibrates how strongly the draw probability decays with team imbalance.

---

## Motivation factors (实时战意)

Computed from real group standings:

| Status | λ factor | When applied |
|---|---|---|
| `qualified_top2` | 0.92 | Group stage finished, team in top 2 — may rotate |
| `near_qualified` | 0.94 | Top of group with 6+ pts, last match |
| `fighting` | 1.03 | Normal group stage play |
| `must_win` | 1.08 | Top of group, last match, ≤3 pts |
| `fighting_3rd` | 1.02 | 3rd place, 3+ pts — fighting for 8 best 3rds |
| `eliminated` | 0.82 | Group stage finished, rank 3-4 with <3 pts |

Asymmetric motivation tilts the λ values, e.g. an eliminated team playing a must-win team sees the must-win team's λ amplified.

---

## Self-evolving loop

`self_evolving_loop.py` runs daily at 14:00 BJT:

1. **step1_match** — scrape completed match results, append to `wc_results.json`
2. **step2_standings** — recompute group standings + motivation
3. **step3_reconcile** — match historical predictions with actual results, log Brier per match
4. **step4_history** — append to `prediction_history.json` (with keys for backtest lookup)
5. **step5_learn** — fit optimal fusion weights on 30-day rolling window, retrain LightGBM if drift detected

Daily retrain results stored in `data/loop.log` and visible at `/api/retrain`.

---

## Cron

`crontab.example`:

```cron
# Sporttery odds relay (every 2 min — iPhone UA has no rate limit)
*/2 * * * * /home/hetaisheng/soccerdata/wc_analysis/relay_sporttery.sh

# Daily retrain at 14:00 BJT
0 14 * * * cd /home/hetaisheng/soccerdata && source .venv/bin/activate && python wc_analysis/predict.py --retrain >> wc_analysis/data/loop.log 2>&1

# Standings update every 30 min during matches
*/30 * * * * cd /home/hetaisheng/soccerdata && source .venv/bin/activate && python wc_analysis/standings.py >> wc_analysis/data/standings.log 2>&1
```

---

## Deployment

Production setup (this repo's live deployment):

- **VPS**: `ubuntu@170.106.198.250:8026` (Singapore, public via nginx)
- **4090 box**: `hts@110os9214fc69.vicp.fun:41380` (China residential IP, GPU)
- **Domain**: `predict.hetaisheng.ccwu.cc` (Cloudflare DNS)

Steps:
1. Clone repo to both machines
2. On VPS: `bash wc_analysis/deploy_vps.sh` (installs deps, starts systemd service)
3. On 4090 box: `python3 wc_analysis/scrape_sporttery.py` works out of the box
4. On local: set up crontab from `crontab.example`
5. Verify: `curl https://predict.hetaisheng.ccwu.cc/data/standings.json` returns 200

---

## Disclaimer

This project is for **research and educational purposes only**. Sports betting involves substantial financial risk. Past model performance does not guarantee future results. The authors do not encourage or endorse gambling. Use at your own risk.

The model outputs are probabilities, not predictions of actual outcomes. Even with edge > 5% and EV > 10%, individual bets can lose due to variance. Kelly fractions are conservative (¼ Kelly) and should be adjusted to your personal risk tolerance.

---

## Acknowledgments

- **soccerdata** by Pieter Robberechts (KU Leuven) — base scraper library this project builds on
- **Dixon-Coles (1997)** — foundational paper on football score modeling
- **Elo** — Arpad Elo's rating system, adapted for football by eloratings.net
- **中国竞彩官方** (sporttery.cn) — the source of market odds
- **Wikipedia** — WC 2026 group composition reference

---

## License

This project is licensed under the Apache License 2.0 — see `LICENSE.rst`.

The base `soccerdata` library is also Apache 2.0 (Pieter Robberechts, KU Leuven). The custom `wc_analysis/` work in this repository is © 2026 the author.

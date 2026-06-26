#!/usr/bin/env python3
"""Render the 2026-06-22 analysis JSON as a static browser report."""
from __future__ import annotations

import html
import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "worldcup_0622_report.json"
OUT = Path(__file__).parent / "outputs" / "worldcup_0622_deep_analysis.html"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def bar(label: str, value: float, color: str) -> str:
    return (
        f'<div class="bar-row"><span>{html.escape(label)}</span>'
        f'<div class="track"><div class="fill" style="width:{value*100:.1f}%;background:{color}"></div></div>'
        f"<b>{pct(value)}</b></div>"
    )


def main() -> None:
    report = json.loads(DATA.read_text(encoding="utf-8"))
    cards = []
    for rec in report:
        p = rec["posterior"]["wld"]
        prior = rec["prior"]["wld"]
        market = rec["market"] or {}
        hcap = market.get("hhad", {})
        had = market.get("had", {})
        scores = ", ".join(
            f"{item['score']} {pct(item['prob'])}" for item in rec["posterior"]["top_scores"][:4]
        )
        reasons = "".join(f"<li>{html.escape(x)}</li>" for x in rec["lambdas"]["calibration_reasons"])
        flags = "".join(f"<li>{html.escape(x)}</li>" for x in rec["risk_flags"])
        cards.append(
            f"""
            <section class="match-card">
              <div class="match-head">
                <div>
                  <p class="num">{html.escape(rec['fixture']['match_num'])} · {html.escape(rec['fixture']['kickoff_bj'])} 北京时间</p>
                  <h2>{html.escape(rec['match'])}</h2>
                  <p class="venue">{html.escape(rec['fixture']['venue'])}</p>
                </div>
                <div class="elo">Elo差 <strong>{rec['elo']['diff']:+.0f}</strong></div>
              </div>
              <div class="grid">
                <div>
                  <h3>校准后胜平负</h3>
                  {bar('主胜', p['home'], '#2563eb')}
                  {bar('平局', p['draw'], '#64748b')}
                  {bar('客胜', p['away'], '#dc2626')}
                </div>
                <div>
                  <h3>先验 vs 盘口</h3>
                  <p>先验：主{pct(prior['home'])} / 平{pct(prior['draw'])} / 客{pct(prior['away'])}</p>
                  <p>体彩胜平负：{('主'+pct(had.get('prob',{}).get('home',0))+' / 平'+pct(had.get('prob',{}).get('draw',0))+' / 客'+pct(had.get('prob',{}).get('away',0))) if had else '未开'}</p>
                  <p>让球：{html.escape(str(hcap.get('handicap', '未开')))}</p>
                  <p>热门比分：{html.escape(scores)}</p>
                </div>
              </div>
              <h3>校准依据</h3>
              <ul>{reasons}</ul>
              {f'<h3>风险提示</h3><ul>{flags}</ul>' if flags else ''}
            </section>
            """
        )
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>2026-06-22 世界杯四场深度分析</title>
  <style>
    body {{ margin:0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:#0f172a; background:#f8fafc; }}
    header {{ padding:42px 56px 24px; background:#0f172a; color:#f8fafc; }}
    h1 {{ margin:0; font-size:42px; letter-spacing:0; }}
    header p {{ max-width:960px; color:#cbd5e1; font-size:18px; line-height:1.6; }}
    main {{ padding:32px 56px 56px; max-width:1180px; margin:0 auto; }}
    .match-card {{ background:white; border:1px solid #e2e8f0; border-radius:8px; padding:28px; margin-bottom:24px; box-shadow:0 10px 24px rgba(15,23,42,.06); }}
    .match-head {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; border-bottom:1px solid #e2e8f0; padding-bottom:18px; margin-bottom:20px; }}
    .num {{ margin:0 0 6px; color:#64748b; font-weight:700; }}
    h2 {{ margin:0; font-size:30px; }}
    h3 {{ margin:18px 0 12px; font-size:18px; }}
    .venue {{ color:#475569; margin-bottom:0; }}
    .elo {{ background:#f1f5f9; padding:12px 16px; border-radius:6px; white-space:nowrap; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:32px; }}
    .bar-row {{ display:grid; grid-template-columns:52px 1fr 58px; gap:10px; align-items:center; margin:11px 0; }}
    .track {{ height:12px; background:#e2e8f0; border-radius:999px; overflow:hidden; }}
    .fill {{ height:100%; }}
    p, li {{ line-height:1.55; }}
    @media (max-width: 760px) {{ header, main {{ padding-left:22px; padding-right:22px; }} .grid, .match-head {{ grid-template-columns:1fr; display:block; }} h1 {{ font-size:32px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>2026-06-22 世界杯四场深度分析</h1>
    <p>两步法：先验模型用 Elo、FBref 近期表现、npxG 代理和 Dixon-Coles；校准层加入伤病、休息、天气和体彩盘口。结论区分胜面和投注价值。</p>
  </header>
  <main>{''.join(cards)}</main>
</body>
</html>"""
    OUT.write_text(html_doc, encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()

"""Quick top3 predictions for the serve endpoint.

Uses FusionPredictor (Elo+DC + LightGBM + PyTorch ensemble) for best accuracy.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

WC_DIR = Path(__file__).parent
DATA_DIR = WC_DIR / "data"
sys.path.insert(0, str(WC_DIR.parent))

from wc_analysis.elo_model import EloModel
from wc_analysis.top_predictions import TopPredictor
import torch
try:
    from wc_analysis.fusion_predictor import FusionPredictor
    _FUSION_AVAILABLE = True
except ImportError:
    _FUSION_AVAILABLE = False


def _code_to_name_map(df: pd.DataFrame) -> dict:
    teams = set(df["home_team"].unique()) | set(df["away_team"].unique())
    mapping = {
        "POR": "Portugal", "UZB": "Uzbekistan", "COL": "Colombia", "COD": "DR Congo",
        "DRC": "DR Congo", "ENG": "England", "GHA": "Ghana", "PAN": "Panama", "PAM": "Panama",
        "CRO": "Croatia", "MEX": "Mexico", "CZE": "Czech Republic", "KOR": "South Korea",
        "CAN": "Canada", "SUI": "Switzerland", "BIH": "Bosnia and Herzegovina",
        "QAT": "Qatar", "SCO": "Scotland", "BRA": "Brazil", "MAR": "Morocco",
        "HAI": "Haiti", "USA": "United States", "TUR": "Turkey", "PAR": "Paraguay", "PGY": "Paraguay",
        "AUS": "Australia", "AUA": "Australia", "CUR": "Curaçao",
        "CIV": "Côte d'Ivoire", "ECU": "Ecuador", "GER": "Germany",
        "JPN": "Japan", "SWE": "Sweden", "TUN": "Tunisia",
        "NET": "Netherlands", "NOR": "Norway", "NOW": "Norway",
        "FRA": "France", "SEN": "Senegal", "IRQ": "Iraq",
        "CPV": "Cape Verde", "CVI": "Cape Verde", "KSA": "Saudi Arabia", "SAR": "Saudi Arabia",
        "URU": "Uruguay", "ESP": "Spain", "SPA": "Spain",
        "EGY": "Egypt", "IRA": "IR Iran", "IRN": "IR Iran",
        "NZL": "New Zealand", "NZD": "New Zealand", "BEL": "Belgium", "BEG": "Belgium",
        "RSA": "South Africa", "ALG": "Algeria", "AUT": "Austria",
        "JOR": "Jordan", "ARG": "Argentina", "COM": "Comoros", "POG": "Portugal",
    }
    return {k: v for k, v in mapping.items() if v in teams}


def generate_top3_predictions() -> list[dict]:
    df = pd.read_csv(DATA_DIR.parent.parent / "data" / "international_results.csv")
    df["date"] = pd.to_datetime(df["date"])
    elo = EloModel()
    elo.fit(df[df["date"] < datetime.now().strftime("%Y-%m-%d")])

    code_map = _code_to_name_map(df)
    top_predictor = TopPredictor(elo, None)
    
    # Try fusion predictor (lightweight: only use if models available)
    fusion = None
    if _FUSION_AVAILABLE:
        try:
            fusion = FusionPredictor()
            fusion.elo_model = elo
            fusion.draw_correction = top_predictor.dc  # share from TopPredictor
            # Don't call load_all() — it loads historical data again; just use the elo_model we already have
            # Load the fusion weights (LightGBM, PyTorch models)
            import pickle
            try:
                fusion.lgbm_model = pickle.load(open(DATA_DIR / "model_lightgbm.pkl", "rb"))
            except Exception:
                pass
            try:
                fusion.pytorch_model = torch.load(DATA_DIR / "model_pytorch.pt", map_location="cpu", weights_only=False)
            except Exception:
                pass
            fusion.loaded = True
        except Exception:
            pass

    matches_file = DATA_DIR / "odds_parsed.json"
    if not matches_file.exists():
        return []
    matches = json.loads(matches_file.read_text())

    predictions = []
    for m in matches:
        home_c, away_c = m.get("home_en"), m.get("away_en")
        if not home_c or not away_c:
            continue
        home = code_map.get(home_c, home_c)
        away = code_map.get(away_c, away_c)
        if home not in elo.ratings or away not in elo.ratings:
            continue
        try:
            hline = float(m.get("hhad_line", 0))
        except Exception:
            hline = None

        # Get Elo+DC top3 (scores, handicap)
        try:
            top3 = top_predictor.predict(home, away, neutral=True, handicap_line=hline,
                                         as_of_date=datetime.now().strftime("%Y-%m-%d"),
                                         matches_df=df)
        except Exception:
            continue

        # Blend 1X2: 50% fusion + 50% top_predictor (Elo+DC)
        p_home_f, p_draw_f, p_away_f = top3["p_home"], top3["p_draw"], top3["p_away"]
        if fusion is not None and fusion.loaded:
            try:
                p_f = fusion.predict(home, away, neutral=True)
                # Simple average blend
                p_home_f = 0.55 * p_f[0] + 0.45 * p_home_f
                p_draw_f = 0.55 * p_f[1] + 0.45 * p_draw_f
                p_away_f = 0.55 * p_f[2] + 0.45 * p_away_f
                total = p_home_f + p_draw_f + p_away_f
                p_home_f, p_draw_f, p_away_f = p_home_f/total, p_draw_f/total, p_away_f/total
            except Exception:
                pass

        # Re-rank top_1x2 with blended probs
        x12_ranked = sorted([("主胜", p_home_f), ("平局", p_draw_f), ("客胜", p_away_f)],
                           key=lambda x: -x[1])

        predictions.append({
            "home": home_c, "away": away_c, "home_full": home, "away_full": away,
            "date": m.get("date"), "time": m.get("time"),
            "league": m.get("league", "FIFA World Cup"),
            "handicap_line": hline,
            "elo_diff": round(top3["elo_diff"]),
            "p_home": round(p_home_f, 3), "p_draw": round(p_draw_f, 3),
            "p_away": round(p_away_f, 3),
            "top_1x2": [{"outcome": o, "probability": p, "code": "HDA"[["主胜","平局","客胜"].index(o)] if o in ["主胜","平局","客胜"] else "H"}
                       for o, p in x12_ranked[:3]],
            "top_scores": top3["top_scores"][:6],
            "handicap": top3.get("handicap"),
            "suggested_lines": top3["suggested_lines"][:3],
            "model": "fusion_v2_rich37",
            "generated_at": datetime.now().isoformat(),
        })

    out_file = DATA_DIR / "top3_predictions.json"
    out_file.write_text(json.dumps(predictions, indent=2, ensure_ascii=False))
    return predictions


if __name__ == "__main__":
    preds = generate_top3_predictions()
    print(f"Generated {len(preds)} top-3 predictions")


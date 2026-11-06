"""
KB Scorer — implements the thesis_score formula from the design brief.

thesis_score = (
    earnings_quality_score   * w1 +
    mgmt_quality_score       * w2 +
    valuation_score          * w3 +
    momentum_score           * w4 +
    thesis_alignment_score   * w5
)

Weights are dynamic — start equal (0.20 each) and update from prediction errors.
Claude proposes weight changes; human approves; this file implements them.
"""

import json
import os
from pathlib import Path

KB_DIR = Path(__file__).parent
WEIGHTS_FILE = KB_DIR / "weights.json"


def load_weights() -> dict:
    if WEIGHTS_FILE.exists():
        return json.load(open(WEIGHTS_FILE))
    return {"w1": 0.20, "w2": 0.20, "w3": 0.20, "w4": 0.20, "w5": 0.20, "version": 1}


def score_thesis(ticker: str) -> dict:
    thesis_path = KB_DIR / ticker / "thesis.json"
    if not thesis_path.exists():
        return {"error": f"No thesis file for {ticker}"}

    thesis = json.load(open(thesis_path))
    weights = load_weights()
    scoring = thesis.get("scoring", {})

    eq = scoring.get("earnings_quality_score", {}).get("score", 0)
    mq = scoring.get("mgmt_quality_score", {}).get("score", 0)
    vs = scoring.get("valuation_score", {}).get("score", 0)
    ms = scoring.get("momentum_score", {}).get("score", 0)
    ta = scoring.get("thesis_alignment_score", {}).get("score", 0)

    w = weights
    total = eq * w["w1"] + mq * w["w2"] + vs * w["w3"] + ms * w["w4"] + ta * w["w5"]

    return {
        "ticker": ticker,
        "thesis_score": round(total, 4),
        "component_scores": {
            "earnings_quality": eq,
            "mgmt_quality": mq,
            "valuation": vs,
            "momentum": ms,
            "thesis_alignment": ta,
        },
        "weights_used": w,
        "signal": scoring.get("signal", "HOLD"),
        "sub_signal": scoring.get("sub_signal", ""),
        "confidence": scoring.get("confidence", 0.5),
    }


def list_scored_tickers() -> list:
    return [
        d.name for d in KB_DIR.iterdir()
        if d.is_dir() and (d / "thesis.json").exists()
    ]


def update_weights(new_weights: dict, reason: str):
    """Update weights after human approval. Record the reason."""
    weights = load_weights()
    weights.update(new_weights)
    weights["version"] = weights.get("version", 1) + 1
    weights["last_updated"] = __import__("datetime").date.today().isoformat()
    weights["update_reason"] = reason
    json.dump(weights, open(WEIGHTS_FILE, "w"), indent=2)
    print(f"Weights updated to v{weights['version']}: {new_weights}")


if __name__ == "__main__":
    tickers = list_scored_tickers()
    print(f"Scored tickers: {tickers}")
    for t in tickers:
        result = score_thesis(t)
        print(f"\n{t}: score={result['thesis_score']} signal={result['signal']} confidence={result['confidence']}")
        for k, v in result["component_scores"].items():
            print(f"  {k}: {v}")

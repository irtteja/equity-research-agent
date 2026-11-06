# KB — Knowledge Base

One thesis file per company. This is the persistent, structured, graph-linked store of investment theses.

## Structure

```
kb/
├── README.md           — this file
├── scorer.py           — mathematical scoring engine
├── weights.json        — dynamic weights (updated from prediction errors)
└── SYMBOL/
    └── thesis.json     — per-company thesis file
```

## Thesis file schema

Each `thesis.json` contains:

| Field | Description |
|---|---|
| `core_thesis` | Bull case + bear case + one-line summary |
| `scoring` | Five component scores (0-1) + thesis_score + signal |
| `evidence_log` | Timestamped, sourced facts that informed the thesis |
| `prediction_log` | What was predicted, what happened, outcome measured |
| `entity_relationships` | Competitors, macro factors, index memberships |
| `open_questions` | Active research gaps the system is looking for |
| `watch_triggers` | Price/event conditions that warrant thesis revision |

## Scoring formula

```python
thesis_score = (
    earnings_quality_score   * w1 +
    mgmt_quality_score       * w2 +
    valuation_score          * w3 +
    momentum_score           * w4 +
    thesis_alignment_score   * w5
)
```

Weights start at 0.20 each. Updated via `kb/scorer.py::update_weights()` after human approval, based on 90-day prediction outcomes.

## Lifecycle

```
Thesis created → evidence accumulates → prediction made
         ↓
90 days later → outcome measured → error attributed
         ↓
Weight update proposed → human approves → weights updated
         ↓
Scoring improves over time
```

## Covered tickers

Run `python3 kb/scorer.py` to see all scored tickers and their current signals.

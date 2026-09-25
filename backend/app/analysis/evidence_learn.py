"""Learning from outcomes (Phase 10): which evidence factors actually predicted winners.

Every stored setup (the buys that were shown, and the ones a filter held back, tracked the same
way) carries its evidence board. Once they close, this module measures:

* per factor: the average result when it argued for the trade versus against it (its "edge");
* per grade: how "strong support" boards did versus "headwinds";
* a logistic model on all factor values together, trained on the older closed setups and
  validated on the newer ones with the same rules as the Phase 9 filter (app.analysis.ml): it
  is used only when it separates winners from losers on data it never saw, and then only to
  lower a buy to WATCH.

Until enough setups have closed (MIN_TRAIN + MIN_TEST in ml), the board uses its prior weights
and this module only reports what it has seen so far.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.analysis import ml
from app.analysis.evidence import GRADE_TEXT, LABELS, PRIOR_WEIGHTS

TRAIN_FRACTION = 0.7
SIDE_THRESHOLD = 0.1
HORIZON_FEATURES = ("h_swing", "h_15m", "h_1h", "h_4h")


@dataclass
class Sample:
    time: datetime
    horizon: str  # swing | 15m | 1h | 4h
    features: dict[str, float]  # signed factor values
    grade: str
    score: float | None
    r_multiple: float
    shown: bool  # True: shown as a buy; False: held back by a filter
    filtered_by: str | None = None


def model_features(features: dict[str, float], horizon: str, score: float | None) -> dict[str, float]:
    out = {k: float(features.get(k, 0.0)) for k in PRIOR_WEIGHTS}
    out["score"] = (score or 0.0) / 100.0
    for h in HORIZON_FEATURES:
        out[h] = 1.0 if h == f"h_{horizon}" else 0.0
    return out


def stats_of(rs: Sequence[float]) -> dict[str, Any]:
    if not rs:
        return {"n": 0, "win_rate": None, "avg_r": None, "total_r": 0.0}
    return {"n": len(rs), "win_rate": 100.0 * sum(1 for r in rs if r > 0) / len(rs), "avg_r": statistics.fmean(rs),
            "total_r": math.fsum(rs)}


def factor_table(samples: Sequence[Sample]) -> list[dict[str, Any]]:
    rows = []
    for key in PRIOR_WEIGHTS:
        pro = [s.r_multiple for s in samples if s.features.get(key, 0.0) > SIDE_THRESHOLD]
        con = [s.r_multiple for s in samples if s.features.get(key, 0.0) < -SIDE_THRESHOLD]
        a, b = stats_of(pro), stats_of(con)
        edge = (a["avg_r"] - b["avg_r"]) if a["n"] >= 5 and b["n"] >= 5 else None
        rows.append({"key": key, "label": LABELS[key], "for": a, "against": b, "edge_r": edge,
                     "verdict": None if edge is None else ("helps" if edge >= 0.15 else "misleads" if edge <= -0.15 else "no clear effect")})
    return rows


def grade_table(samples: Sequence[Sample]) -> list[dict[str, Any]]:
    out = []
    for grade in GRADE_TEXT:
        rs = [s.r_multiple for s in samples if s.grade == grade]
        if rs:
            out.append({"grade": grade, "label": GRADE_TEXT[grade], **stats_of(rs)})
    return out


def train(samples: Sequence[Sample], trained_at: datetime) -> ml.Model | None:
    rows = sorted(
        (ml.Row(model_features(s.features, s.horizon, s.score), s.r_multiple, s.time) for s in samples),
        key=lambda r: r.time,
    )
    if len(rows) < ml.MIN_TRAIN + ml.MIN_TEST:
        return None
    cut = int(len(rows) * TRAIN_FRACTION)
    return ml.train_and_validate(rows[:cut], rows[cut:], "evidence", trained_at)

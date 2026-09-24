"""Statistical trade filter (Phase 9): logistic regression on the market at each entry.

It estimates the probability that a scalp setup ends as a winner (net R above zero) from the
numbers known at the signal candle's close (app.analysis.scalp.features_at). Pure Python, no
dependencies: features are standardised, the model is fitted by Newton's method with an L2
penalty, which suits a few thousand trades and about twenty features.

Honesty rules:
* training uses the older trades only; every reported quality number comes from the newer
  trades the model never saw (time-ordered split, no shuffling);
* the probability threshold is chosen on the training trades, never on the test trades;
* the model counts as validated only if, on the test trades, it ranks winners above losers
  (AUC >= 0.55) and the trades it keeps earned clearly more than all trades together.
An unvalidated model is shown with its numbers but never filters a signal.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

MIN_TRAIN = 60
MIN_TEST = 30
MIN_TEST_KEPT = 15
MIN_AUC = 0.55
MIN_LIFT_R = 0.05
KEEP_FRACTIONS = (0.9, 0.8, 0.7, 0.6, 0.5, 0.4)


@dataclass
class Row:
    """One historical trade: features at entry, its net result in R, and when it happened."""

    features: dict[str, float]
    r_multiple: float
    time: datetime
    symbol: str = ""

    @property
    def win(self) -> int:
        return 1 if self.r_multiple > 0 else 0


@dataclass
class Model:
    horizon: str
    features: list[str]
    means: list[float]
    stds: list[float]
    weights: list[float]  # bias first
    threshold: float
    validated: bool
    metrics: dict[str, Any]
    trained_at: datetime
    reasons: list[str] = field(default_factory=list)

    def probability(self, features: dict[str, float]) -> float:
        z = self.weights[0]
        for name, mean, std, w in zip(self.features, self.means, self.stds, self.weights[1:], strict=True):
            z += w * ((features.get(name, mean) - mean) / std)
        return _sigmoid(z)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["trained_at"] = self.trained_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Model:
        from app.core.timeutil import parse_iso

        return cls(
            horizon=str(data["horizon"]), features=list(data["features"]), means=[float(v) for v in data["means"]],
            stds=[float(v) for v in data["stds"]], weights=[float(v) for v in data["weights"]],
            threshold=float(data["threshold"]), validated=bool(data["validated"]), metrics=dict(data.get("metrics", {})),
            trained_at=parse_iso(data.get("trained_at")) or datetime.min, reasons=list(data.get("reasons", [])),
        )


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve a x = b (Gaussian elimination with partial pivoting); `a` is small and positive definite."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for k in range(col, n + 1):
                    m[r][k] -= f * m[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def fit_logistic(x: Sequence[Sequence[float]], y: Sequence[int], *, l2: float = 1.0, iters: int = 30) -> list[float]:
    """Weights (bias first) of an L2-penalised logistic regression, by Newton-Raphson."""
    d = len(x[0]) + 1
    w = [0.0] * d
    rows = [[1.0, *row] for row in x]
    for _ in range(iters):
        grad = [0.0] * d
        hess = [[0.0] * d for _ in range(d)]
        for row, target in zip(rows, y, strict=True):
            p = _sigmoid(sum(wi * xi for wi, xi in zip(w, row, strict=True)))
            err = p - target
            s = p * (1.0 - p)
            for i in range(d):
                grad[i] += err * row[i]
                si = s * row[i]
                hess_i = hess[i]
                for j in range(i, d):
                    hess_i[j] += si * row[j]
        for i in range(d):
            for j in range(i):
                hess[i][j] = hess[j][i]
        for i in range(1, d):  # no penalty on the bias
            grad[i] += l2 * w[i]
            hess[i][i] += l2
        hess[0][0] += 1e-9
        step = _solve(hess, grad)
        w = [wi - si for wi, si in zip(w, step, strict=True)]
        if max(abs(v) for v in step) < 1e-7:
            break
    return w


def auc(y: Sequence[int], p: Sequence[float]) -> float | None:
    """Probability that a random winner scores above a random loser (0.5 = no skill)."""
    pos = sum(y)
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(p)), key=lambda i: p[i])
    ranks = [0.0] * len(p)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    rank_sum = sum(r for r, t in zip(ranks, y, strict=True) if t == 1)
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def _expectancy(rows: Sequence[Row]) -> float | None:
    return statistics.fmean(r.r_multiple for r in rows) if rows else None


def train_and_validate(train: Sequence[Row], test: Sequence[Row], horizon: str, trained_at: datetime) -> Model | None:
    """Fit on `train` (older trades), report and validate on `test` (newer trades)."""
    if not train or not test:
        return None
    names = sorted(train[0].features)
    x_raw = [[r.features.get(n, 0.0) for n in names] for r in train]
    means = [statistics.fmean(col) for col in zip(*x_raw, strict=True)]
    stds = [max(statistics.pstdev(col), 1e-9) for col in zip(*x_raw, strict=True)]

    def scale(rows: Sequence[Row]) -> list[list[float]]:
        return [[(r.features.get(n, m) - m) / s for n, m, s in zip(names, means, stds, strict=True)] for r in rows]

    y_train = [r.win for r in train]
    reasons: list[str] = []
    try:
        weights = fit_logistic(scale(train), y_train) if 0 < sum(y_train) < len(y_train) else [0.0] * (len(names) + 1)
    except ValueError:
        weights = [0.0] * (len(names) + 1)
        reasons.append("the training data could not be fitted")
    model = Model(horizon, names, means, stds, weights, 0.5, False, {}, trained_at, reasons)
    p_train = [model.probability(r.features) for r in train]
    p_test = [model.probability(r.features) for r in test]

    # threshold: the probability cut that kept the best training trades (never the test trades)
    best: tuple[float, float] | None = None  # (expectancy, threshold)
    for keep in KEEP_FRACTIONS:
        cut = sorted(p_train, reverse=True)[max(0, int(len(p_train) * keep) - 1)]
        kept = [r for r, p in zip(train, p_train, strict=True) if p >= cut]
        exp = _expectancy(kept)
        if exp is not None and (best is None or exp > best[0]):
            best = (exp, cut)
    model.threshold = best[1] if best else 0.5

    kept_test = [r for r, p in zip(test, p_test, strict=True) if p >= model.threshold]
    y_test = [r.win for r in test]
    test_auc = auc(y_test, p_test)
    all_exp, kept_exp = _expectancy(test), _expectancy(kept_test)
    brier = statistics.fmean((p - t) ** 2 for p, t in zip(p_test, y_test, strict=True))
    base_rate = statistics.fmean(y_test)
    model.metrics = {
        "train_trades": len(train), "test_trades": len(test),
        "train_auc": auc(y_train, p_train), "test_auc": test_auc,
        "test_brier": brier, "test_brier_baseline": statistics.fmean((base_rate - t) ** 2 for t in y_test),
        "test_win_rate": 100.0 * base_rate, "test_expectancy_r": all_exp,
        "test_kept": len(kept_test), "test_kept_expectancy_r": kept_exp,
        "test_kept_win_rate": 100.0 * statistics.fmean(r.win for r in kept_test) if kept_test else None,
        "threshold": model.threshold,
        "top_features": sorted(
            ({"name": n, "weight": w} for n, w in zip(names, weights[1:], strict=True)),
            key=lambda f: abs(f["weight"]), reverse=True,
        )[:6],
    }
    if len(train) < MIN_TRAIN:
        reasons.append(f"only {len(train)} training trades (needs {MIN_TRAIN})")
    if len(test) < MIN_TEST:
        reasons.append(f"only {len(test)} test trades (needs {MIN_TEST})")
    if test_auc is None or test_auc < MIN_AUC:
        reasons.append(f"test AUC {test_auc or 0:.2f} below {MIN_AUC}: it does not tell winners from losers well enough")
    if len(kept_test) < MIN_TEST_KEPT:
        reasons.append(f"keeps only {len(kept_test)} test trades (needs {MIN_TEST_KEPT})")
    if kept_exp is None or all_exp is None or kept_exp <= max(0.0, all_exp + MIN_LIFT_R):
        reasons.append(
            f"kept test trades earned {kept_exp or 0:+.2f}R vs {all_exp or 0:+.2f}R for all: not a clear improvement"
        )
    model.validated = not reasons
    model.reasons = reasons or [
        f"validated on {len(test)} newer trades: AUC {test_auc:.2f}, kept trades {kept_exp:+.2f}R vs {all_exp:+.2f}R"
    ]
    return model

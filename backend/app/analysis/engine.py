"""Signal engine: runs the Phase 2 pipeline for one asset.

integrity gate (Phase 1) -> ANALYSIS_CHECK (indicators, structure, regime)
-> quantitative score -> trade plan -> RISK_CHECK -> market regime cap
-> FINAL_VALIDATION -> label

The label can only be lowered after scoring, never raised. A failed integrity gate, a
failed analysis check, a blocking risk check or a final-validation violation always
yields NO TRADE.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.analysis.features import FEATURE_VERSION, IndicatorSnapshot, compute_snapshot
from app.analysis.finalize import final_validation
from app.analysis.regime import MarketRegimeResult, TimeframeRegime, classify_timeframe
from app.analysis.scoring import Factor, ScoreInputs, SignalParams, label_for_score, score_factors, total_score
from app.analysis.structure import StructureAnalysis, analyze_structure
from app.core.enums import DataState, GateStage, RiskSeverity, SignalLabel, Timeframe, TrendDirection, VolatilityLevel
from app.core.formatting import fmt_price
from app.data.normalization.schemas import Candle
from app.data.validation.gate import IntegrityResult
from app.data.validation.orderbook import OrderBookSummary
from app.risk.engine import RiskCheck, RiskInputs, apply_risk, evaluate_risk
from app.risk.params import RiskParams
from app.risk.plan import TradePlan, build_plan, overhead_resistances

ENGINE_VERSION = "quant-2.0.0"
STRATEGY = "mtf_trend_pullback"
SETUP_TIMEFRAME = Timeframe.H4
STRUCTURE_TIMEFRAMES = (Timeframe.H1, Timeframe.H4, Timeframe.D1)

# Indicator values the score cannot do without, per timeframe.
REQUIRED_FIELDS: dict[Timeframe, tuple[str, ...]] = {
    Timeframe.D1: ("ema50", "ema200", "rsi14", "atr14", "roc20"),
    Timeframe.H4: ("ema20", "ema50", "ema200", "rsi14", "macd_hist", "atr14", "adx14", "bb_pct_b"),
    Timeframe.H1: ("ema20", "ema50", "rsi14", "macd_hist"),
    Timeframe.M15: ("rsi14",),
}


@dataclass(frozen=True)
class EngineParams:
    signal: SignalParams = field(default_factory=SignalParams)
    risk: RiskParams = field(default_factory=RiskParams)


@dataclass
class AnalysisInputs:
    symbol: str
    name: str
    universe_rank: int
    now: datetime
    supported: bool
    unsupported_reason: str | None
    candles: dict[Timeframe, list[Candle]]  # validated closed candles
    price: float | None  # live ticker price in the quote asset
    quote_asset: str | None
    market_source: str | None
    quote_usd_rate: float | None
    pct_change_24h: float | None
    volume_24h_quote: float | None
    order_book: OrderBookSummary | None
    integrity: IntegrityResult | None
    market: MarketRegimeResult


@dataclass
class PipelineStage:
    stage: str
    passed: bool
    outcome: str  # PASS | CAP | DOWNGRADE | BLOCK | NOT_RUN
    reasons: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    symbol: str
    name: str
    universe_rank: int
    generated_at: datetime
    signal: SignalLabel
    score: int
    score_label: SignalLabel
    trend: str
    market: MarketRegimeResult
    price: float | None
    quote_asset: str | None
    market_source: str | None
    data_state: DataState
    data_health_score: int
    integrity_passed: bool
    pipeline: list[PipelineStage]
    factors: list[Factor]
    snapshots: dict[Timeframe, IndicatorSnapshot]
    structures: dict[Timeframe, StructureAnalysis]
    regimes: dict[Timeframe, TimeframeRegime]
    plan: TradePlan | None
    risk_checks: list[RiskCheck]
    reasons: list[str]
    risks: list[str]
    summary: str
    setup_candle_open_time: datetime | None
    engine_version: str = ENGINE_VERSION
    feature_version: str = FEATURE_VERSION
    strategy: str = STRATEGY
    setup_timeframe: Timeframe = SETUP_TIMEFRAME


def _analysis_check(
    price: float | None, snapshots: dict[Timeframe, IndicatorSnapshot], structures: dict[Timeframe, StructureAnalysis]
) -> list[str]:
    problems: list[str] = []
    if price is None or not math.isfinite(price) or price <= 0:
        problems.append("live price unavailable")
    for tf, names in REQUIRED_FIELDS.items():
        snap = snapshots.get(tf)
        if snap is None:
            problems.append(f"{tf.label}: not enough closed candles for indicators")
            continue
        missing = [n for n in names if (v := getattr(snap, n)) is None or not math.isfinite(v)]
        if missing:
            problems.append(f"{tf.label}: {', '.join(missing)} unavailable (needs more history)")
    if Timeframe.H4 not in structures:
        problems.append("4H: market structure unavailable")
    return problems


def _integrity_stages(integrity: IntegrityResult | None) -> list[PipelineStage]:
    if integrity is None:
        return [PipelineStage("DATA_INTEGRITY", False, "BLOCK", ["integrity gate did not run"])]
    return [
        PipelineStage(s.stage.value, s.passed, "PASS" if s.passed else "BLOCK", list(s.reasons))
        for s in integrity.stages
    ]


def _binding_failures(checks: Sequence[RiskCheck], score_label: SignalLabel) -> list[RiskCheck]:
    """Failed checks that actually lowered the label, most severe first."""
    order = {RiskSeverity.BLOCK: 0, RiskSeverity.CAP: 1, RiskSeverity.DOWNGRADE: 2}
    binding = []
    for check in checks:
        if check.passed:
            continue
        if check.severity == RiskSeverity.BLOCK:
            binding.append(check)
        elif check.severity == RiskSeverity.CAP and score_label.rank > SignalLabel.WATCH.rank:
            binding.append(check)
        elif check.severity == RiskSeverity.DOWNGRADE and score_label == SignalLabel.STRONG_BUY:
            binding.append(check)
    return sorted(binding, key=lambda c: order[c.severity])


def _top(factors: Sequence[Factor], *, positive: bool, limit: int) -> list[str]:
    if positive:
        ranked = sorted(factors, key=lambda f: f.score / f.max_score, reverse=True)
        return [f.positives[0] for f in ranked if f.positives][:limit]
    ranked = sorted(factors, key=lambda f: f.max_score - f.score, reverse=True)
    return [f.negatives[0] for f in ranked if f.negatives][:limit]


class SignalEngine:
    def __init__(self, params: EngineParams | None = None) -> None:
        self.params = params or EngineParams()

    def evaluate(self, i: AnalysisInputs) -> AnalysisResult:
        p = self.params
        integrity_passed = i.integrity is not None and i.integrity.passed
        snapshots: dict[Timeframe, IndicatorSnapshot] = {}
        for tf, candles in i.candles.items():
            snap = compute_snapshot(tf, candles)
            if snap is not None:
                snapshots[tf] = snap
        structures: dict[Timeframe, StructureAnalysis] = {}
        for tf in STRUCTURE_TIMEFRAMES:
            if tf in i.candles and tf in snapshots:
                analysis = analyze_structure(tf, i.candles[tf], snapshots[tf].atr14)
                if analysis is not None:
                    structures[tf] = analysis
        regimes = {tf: classify_timeframe(s) for tf, s in snapshots.items()}

        pipeline = _integrity_stages(i.integrity)
        analysis_problems = (
            [i.unsupported_reason or "no supported spot market"] if not i.supported
            else _analysis_check(i.price, snapshots, structures)
        )
        analysis_passed = not analysis_problems
        pipeline.append(
            PipelineStage(GateStage.ANALYSIS_CHECK.value, analysis_passed, "PASS" if analysis_passed else "BLOCK",
                          analysis_problems)
        )

        factors: list[Factor] = []
        plan: TradePlan | None = None
        trend_1d = regimes[Timeframe.D1].trend if Timeframe.D1 in regimes else None
        trend_4h = regimes[Timeframe.H4].trend if Timeframe.H4 in regimes else None
        if analysis_passed and i.price is not None:
            h4 = snapshots[Timeframe.H4]
            atr = h4.atr14 or 0.0
            resistances = overhead_resistances(
                i.price, atr, [structures.get(Timeframe.H4), structures.get(Timeframe.D1)]
            )
            factors = score_factors(
                ScoreInputs(
                    price=i.price,
                    snapshots=snapshots,
                    structures=structures,
                    market=i.market,
                    is_btc=i.symbol.upper() == "BTC",
                    resistances=resistances,
                )
            )
            if trend_4h != TrendDirection.DOWN:
                plan = build_plan(
                    price=i.price,
                    quote_asset=i.quote_asset or "",
                    h4=h4,
                    structure_4h=structures.get(Timeframe.H4),
                    resistances=resistances,
                    params=p.risk,
                )
        score = total_score(factors)
        score_label = label_for_score(score, p.signal) if analysis_passed else SignalLabel.NO_TRADE

        risk_checks = evaluate_risk(
            RiskInputs(
                price=i.price,
                quote_usd_rate=i.quote_usd_rate,
                order_book=i.order_book,
                volume_24h_quote=i.volume_24h_quote,
                pct_change_24h=i.pct_change_24h,
                h4=snapshots.get(Timeframe.H4),
                d1=snapshots.get(Timeframe.D1),
                m15=snapshots.get(Timeframe.M15),
                trend_1d=trend_1d,
                trend_4h=trend_4h,
                plan=plan,
                market=i.market,
            ),
            p.risk,
        )
        label = apply_risk(score_label, risk_checks)
        failed = [c for c in risk_checks if not c.passed]
        risk_blocked = any(c.severity == RiskSeverity.BLOCK for c in failed)
        severities = {c.severity for c in failed}
        outcome = (
            "BLOCK" if RiskSeverity.BLOCK in severities
            else "CAP" if RiskSeverity.CAP in severities
            else "DOWNGRADE" if RiskSeverity.DOWNGRADE in severities
            else "PASS"
        )
        pipeline.append(PipelineStage(GateStage.RISK_CHECK.value, not risk_blocked, outcome, [c.detail for c in failed]))

        if not integrity_passed or not analysis_passed:
            label = SignalLabel.NO_TRADE
        problems = final_validation(
            label,
            plan,
            integrity_passed=integrity_passed,
            analysis_passed=analysis_passed,
            risk_blocked=risk_blocked,
            params=p.risk,
        )
        if problems:
            label = SignalLabel.NO_TRADE
        pipeline.append(
            PipelineStage(GateStage.FINAL_VALIDATION.value, not problems, "BLOCK" if problems else "PASS", problems)
        )

        if plan is not None:
            plan.actionable = label in (SignalLabel.BUY, SignalLabel.STRONG_BUY)
        if label == SignalLabel.NO_TRADE:
            plan = None

        reasons = self._reasons(
            i, label, score, score_label, analysis_problems, risk_checks, problems, factors, plan, trend_1d, trend_4h
        )
        risks = self._risks(i, snapshots, regimes, plan)
        trend = "n/a"
        if trend_1d is not None and trend_4h is not None:
            trend = trend_1d.value if trend_1d == trend_4h else "MIXED"
        h4_snap = snapshots.get(Timeframe.H4)
        return AnalysisResult(
            symbol=i.symbol,
            name=i.name,
            universe_rank=i.universe_rank,
            generated_at=i.now,
            signal=label,
            score=score,
            score_label=score_label,
            trend=trend,
            market=i.market,
            price=i.price,
            quote_asset=i.quote_asset,
            market_source=i.market_source,
            data_state=i.integrity.state if i.integrity else DataState.API_FAILURE,
            data_health_score=i.integrity.data_health_score if i.integrity else 0,
            integrity_passed=integrity_passed,
            pipeline=pipeline,
            factors=factors,
            snapshots=snapshots,
            structures=structures,
            regimes=regimes,
            plan=plan,
            risk_checks=risk_checks,
            reasons=reasons,
            risks=risks,
            summary=self._summary(label, score, reasons, plan),
            setup_candle_open_time=h4_snap.open_time if h4_snap else None,
        )

    def _reasons(
        self,
        i: AnalysisInputs,
        label: SignalLabel,
        score: int,
        score_label: SignalLabel,
        analysis_problems: list[str],
        checks: list[RiskCheck],
        final_problems: list[str],
        factors: list[Factor],
        plan: TradePlan | None,
        trend_1d: TrendDirection | None,
        trend_4h: TrendDirection | None,
    ) -> list[str]:
        if not i.supported:
            return [i.unsupported_reason or "no supported spot market"]
        if i.integrity is None or not i.integrity.passed:
            gate = i.integrity.reasons if i.integrity else ["integrity gate did not run"]
            return [f"data integrity: {r}" for r in gate[:4]] or ["data integrity gate failed"]
        if analysis_problems:
            return [f"analysis: {r}" for r in analysis_problems[:4]]
        if final_problems:
            return [f"final validation: {r}" for r in final_problems]
        binding = _binding_failures(checks, score_label)
        positives = _top(factors, positive=True, limit=5)
        if label in (SignalLabel.BUY, SignalLabel.STRONG_BUY) and plan is not None:
            if trend_1d == TrendDirection.UP and trend_4h == TrendDirection.UP:
                trend_text = "Uptrend on 1D and 4H"
            elif trend_4h == TrendDirection.UP:
                trend_text = "4H uptrend, 1D not down"
            else:
                trend_text = "1D and 4H not in a downtrend"
            location_factor = next((f for f in factors if f.key == "location"), None)
            location = next((t for t in (location_factor.positives if location_factor else []) if "EMA" in t), None)
            tp2 = plan.targets[1]
            rr_text = f"{plan.reward_risk:.1f}R net to TP2" + (" (projection)" if tp2.projected else "")
            reasons = ["; ".join(part for part in (trend_text, location, rr_text) if part)]
            reasons += [f"not STRONG BUY: {c.detail}" for c in binding]
            reasons += [text for text in positives if text != location][:4]
            return reasons
        reasons = [c.detail for c in binding]
        if score_label.rank < SignalLabel.BUY.rank:
            reasons.extend(_top(factors, positive=False, limit=3))
            reasons.append(f"score {score}/100 is below the BUY threshold ({self.params.signal.min_score_buy:g})")
        elif label.rank < score_label.rank and not reasons:
            reasons.append(f"score {score}/100 limited by risk checks")
        reasons.extend(positives[:3])
        return reasons

    def _risks(
        self,
        i: AnalysisInputs,
        snapshots: dict[Timeframe, IndicatorSnapshot],
        regimes: dict[Timeframe, TimeframeRegime],
        plan: TradePlan | None,
    ) -> list[str]:
        risks: list[str] = []
        m = i.market
        if "HIGH_VOLATILITY" in m.flags:
            risks.append("Bitcoin daily volatility is high")
        fg = m.fear_greed
        if fg is not None and "EXTREME_GREED" in m.flags:
            risks.append(f"Fear & Greed {fg.value} (extreme greed, {fg.source})")
        if fg is not None and "EXTREME_FEAR" in m.flags:
            risks.append(f"Fear & Greed {fg.value} (extreme fear, {fg.source})")
        h4 = regimes.get(Timeframe.H4)
        if h4 is not None and h4.volatility == VolatilityLevel.HIGH:
            risks.append(f"4H volatility high (ATR at the {h4.atr_pct_percentile or 0:.0f}th percentile)")
        if i.integrity is not None and i.integrity.passed:
            for stage in i.integrity.stages:
                if stage.state == DataState.DEGRADED:
                    risks.extend(f"data: {r}" for r in stage.reasons[:2])
        if plan is not None:
            risks.extend(plan.notes)
            m15 = snapshots.get(Timeframe.M15)
            if m15 is not None and m15.rsi14 is not None and m15.rsi14 > self.params.risk.max_rsi_15m_strong:
                risks.append(f"15m RSI {m15.rsi14:.0f}: short-term spike, prefer the lower part of the entry zone")
        return risks

    @staticmethod
    def _summary(label: SignalLabel, score: int, reasons: list[str], plan: TradePlan | None) -> str:
        if label in (SignalLabel.BUY, SignalLabel.STRONG_BUY) and plan is not None:
            tp1, tp2 = plan.targets[0], plan.targets[1]
            return (
                f"{label.value} (score {score}/100): entry {fmt_price(plan.entry_low)} to {fmt_price(plan.entry_high)} "
                f"{plan.quote_asset}, stop {fmt_price(plan.stop_loss)} (-{plan.stop_distance_pct:.1f}%), "
                f"TP1 {fmt_price(tp1.price)}, TP2 {fmt_price(tp2.price)} ({plan.reward_risk:.1f}R net)"
            )
        head = f"{label.value} (score {score}/100)" if label == SignalLabel.WATCH else label.value
        return f"{head}: {reasons[0]}" if reasons else head

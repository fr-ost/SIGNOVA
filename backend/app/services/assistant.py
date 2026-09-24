"""Context handed to the AI chat assistant: the dashboard's own data, compact and sourced.

Built without new provider calls where possible: the latest scan, cached news and the
stored portfolio. Only an explicitly selected coin is analysed on demand.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.timeutil import utcnow
from app.services.analysis import AnalysisService
from app.services.control import AnalysisController
from app.services.news import NewsService
from app.services.portfolio import PortfolioService
from app.services.universe import UniverseService
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)


def universe_names(universe: UniverseService) -> dict[str, str]:
    last = universe.last
    return {a.symbol: a.name for a in last.assets} if last else {}


def _r(value: float | None, digits: int = 6) -> float | None:
    return float(f"{value:.{digits}g}") if isinstance(value, int | float) else None


async def chat_context(
    analysis: AnalysisService,
    controller: AnalysisController,
    news: NewsService,
    portfolio: PortfolioService,
    watchlist: WatchlistService,
    symbol: str | None,
    sentiment: Any = None,
    onchain: Any = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {"now_utc": utcnow().isoformat(), "watchlist": watchlist.symbols()}
    symbols: set[str] = set()
    scan = controller.last_scan
    if scan is None:
        ctx["latest_scan"] = "No scan has run yet. Suggest pressing 'Analyze now' for fresh signals."
    else:
        m = scan.market_regime
        ctx["market_regime"] = {
            "regime": m.regime.value, "max_signal": m.max_signal.value,
            "btc_trend": m.btc_trend.value if m.btc_trend else None, "breadth_pct": _r(m.breadth_pct, 3),
            "fear_greed": m.fear_greed.value if m.fear_greed else None, "flags": m.flags, "reasons": m.reasons,
        }
        ctx["latest_scan"] = {
            "generated_at": scan.generated_at.isoformat(),
            "counts": scan.counts,
            "signals": [
                {
                    "symbol": r.symbol, "signal": r.signal.value, "score": r.score, "trend": r.trend,
                    "price": _r(r.price), "quote": r.quote_asset, "entry_low": _r(r.entry_low), "entry_high": _r(r.entry_high),
                    "stop": _r(r.stop_loss), "tp1": _r(r.take_profit_1), "tp2": _r(r.take_profit_2),
                    "net_rr_tp2": _r(r.reward_risk, 3), "size_pct": _r(r.suggested_allocation_pct, 3),
                    "reason": r.reasons[0] if r.reasons else r.summary, "watchlist": r.watchlist,
                }
                for r in scan.signals
            ],
        }
        symbols.update(r.symbol for r in scan.signals)

    digest = news.cached()
    if digest is None:
        try:
            digest = await asyncio.wait_for(news.digest(), timeout=15)
        except Exception as exc:  # news is optional context
            log.info("news unavailable for chat", extra={"error": str(exc)})
    if digest is not None:
        ctx["news"] = {
            "fetched_at": digest.fetched_at.isoformat(),
            "headline_sentiment_counts_keyword_method": digest.sentiment,
            "headlines": [
                {"title": n.title, "source": n.source, "published": n.published_at.isoformat() if n.published_at else None,
                 "assets": n.assets, "sentiment": n.sentiment}
                for n in digest.items[:15]
            ],
            "trending_coingecko": [t.symbol for t in digest.trending[:10]],
        }

    mood = sentiment.cached() if sentiment is not None else None
    if mood is not None:
        ctx["market_sentiment"] = {
            "state": mood.state, "score_-1_to_1": mood.score, "trend": mood.trend, "reasons": mood.reasons,
            "crowded_longs_funding": mood.components.get("funding", {}).get("crowded_longs"),
            "coins_with_notes": {s: a.notes for s, a in mood.assets.items() if a.notes},
        }
    chain = onchain.cached() if onchain is not None else None
    if chain is not None:
        ctx["onchain"] = {
            "fetched_at": chain.fetched_at.isoformat(), "btc_network": chain.btc, "eth_network": chain.eth,
            "stablecoin_supply": chain.stablecoins, "labelled_exchange_flows_usd": chain.flows,
            "largest_transfers": [
                {"symbol": w.symbol, "amount": _r(w.amount), "usd": _r(w.amount_usd), "type": w.classification,
                 "from": w.from_label, "to": w.to_label, "time": w.occurred_at.isoformat()}
                for w in chain.whales[:10]
            ],
        }

    ctx["portfolio"] = {
        "cash": portfolio.cash,
        "positions": [
            {"symbol": p.symbol, "quantity": p.quantity, "average_entry": p.average_entry} for p in portfolio.positions.values()
        ],
        "risk_settings": portfolio.risk,
    }

    if symbol:
        try:
            a = await analysis.analyze(symbol)
        except Exception as exc:
            ctx["selected_coin"] = {"symbol": symbol, "error": f"analysis unavailable: {exc}"}
        else:
            symbols.add(a.symbol)
            plan = a.plan
            ctx["selected_coin"] = {
                "symbol": a.symbol, "name": a.name, "signal": a.signal.value, "score": a.score,
                "summary": a.summary, "reasons": a.reasons, "risks": a.risks, "trend": a.trend,
                "price": _r(a.price), "quote": a.quote_asset, "data_state": a.data_state.value,
                "sentiment": a.sentiment,
                "failed_risk_checks": [f"{c.name} ({c.severity.value}): {c.detail}" for c in a.risk_checks if not c.passed],
                "plan": None if plan is None else {
                    "actionable": plan.actionable, "entry_low": _r(plan.entry_low), "entry_high": _r(plan.entry_high),
                    "stop": _r(plan.stop_loss), "stop_basis": plan.stop_basis,
                    "targets": [{"price": _r(t.price), "r": _r(t.r_multiple, 3), "sell_pct": t.allocation_pct, "basis": t.basis} for t in plan.targets],
                    "net_rr_tp2": _r(plan.reward_risk, 3), "size_pct_of_portfolio": _r(plan.suggested_allocation_pct, 3),
                    "invalidation": plan.invalidation,
                },
                "indicators": {
                    i.label: {"close": _r(i.close), "ema20": _r(i.ema20), "ema50": _r(i.ema50), "ema200": _r(i.ema200),
                              "rsi14": _r(i.rsi14, 3), "macd_hist": _r(i.macd_hist, 3), "adx14": _r(i.adx14, 3), "atr_pct": _r(i.atr_pct, 3)}
                    for i in a.indicators
                },
                "structure": {
                    s.label: {"trend": s.trend.value, "supports": [_r(x.price) for x in s.supports],
                              "resistances": [_r(x.price) for x in s.resistances]}
                    for s in a.structure
                },
            }
    ctx["symbols_in_context"] = sorted(symbols)
    return ctx

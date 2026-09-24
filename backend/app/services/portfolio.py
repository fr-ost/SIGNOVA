"""Portfolio, risk settings, allocation, DCA plans and P/L scenarios (Phase 4).

Spot only and manual: the user records cash and positions; nothing is traded. Values are
in the quote currency of the spot markets (USDT on Binance, USD on Kraken; USDT is
treated as USD). Risk settings live in the risk_settings table and are applied to the
signal engine immediately, so position sizes in every signal follow them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import SignalLabel
from app.core.timeutil import utcnow
from app.models import Portfolio, PortfolioPosition, RiskSettings
from app.services.analysis import AnalysisService
from app.services.spot_router import SpotMarketRouter
from app.services.universe import UniverseService
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)

PORTFOLIO_NAME = "default"
SCENARIO_MOVES = (-20.0, -10.0, -5.0, 5.0, 10.0, 20.0)

RISK_LIMITS: dict[str, tuple[float, float]] = {
    "max_risk_per_signal_pct": (0.1, 5.0),
    "initial_allocation_pct": (0.5, 50.0),
    "max_allocation_per_opportunity_pct": (0.5, 50.0),
    "max_dca_allocation_pct": (0.0, 50.0),
    "max_total_open_allocation_pct": (5.0, 100.0),
    "fee_pct": (0.0, 1.0),
    "slippage_pct": (0.0, 1.0),
}
DEFAULT_RISK = {
    "max_risk_per_signal_pct": 1.0,
    "initial_allocation_pct": 5.0,
    "max_allocation_per_opportunity_pct": 10.0,
    "max_dca_allocation_pct": 5.0,
    "max_total_open_allocation_pct": 60.0,
    "fee_pct": 0.1,
    "slippage_pct": 0.05,
}


class PortfolioError(ValueError):
    pass


@dataclass
class Position:
    symbol: str
    quantity: float
    average_entry: float
    notes: str | None = None


def _f(value: Any) -> float:
    return float(value) if value is not None else 0.0


class PortfolioService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        router: SpotMarketRouter,
        universe: UniverseService,
        analysis: AnalysisService,
        watchlist: WatchlistService,
        live_prices: Any = None,
    ) -> None:
        self._sessions = session_factory
        self._router = router
        self._universe = universe
        self._analysis = analysis
        self._watchlist = watchlist
        self._live = live_prices
        self.cash = 0.0
        self.positions: dict[str, Position] = {}
        self.risk: dict[str, float] = dict(DEFAULT_RISK)
        self._portfolio_id: int | None = None
        self._load_lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ storage

    async def load(self, *, force: bool = False) -> None:
        """Create the default portfolio if needed, load it and apply its risk settings (once)."""
        async with self._load_lock:
            if self._loaded and not force:
                return
            await self._load()
            self._loaded = True

    async def _ready(self) -> None:
        if not self._loaded:
            await self.load()

    async def _load(self) -> None:
        if self._sessions is not None:
            try:
                async with self._sessions() as session:
                    portfolio = await self._ensure(session)
                    await session.commit()
                    self.cash = _f(portfolio.cash_balance)
                    rows = (
                        await session.execute(
                            select(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio.id)
                        )
                    ).scalars().all()
                    self.positions = {
                        r.symbol: Position(r.symbol, _f(r.quantity), _f(r.average_entry), r.notes) for r in rows
                    }
                    settings = (
                        await session.execute(select(RiskSettings).where(RiskSettings.portfolio_id == portfolio.id))
                    ).scalar_one()
                    self.risk = {key: _f(getattr(settings, key)) for key in DEFAULT_RISK}
            except Exception:
                log.exception("portfolio load failed; using defaults")
        self._apply_risk()

    async def _ensure(self, session: AsyncSession) -> Portfolio:
        # Startup warmup and the first request may both get here: insert inside a savepoint
        # and re-read on a unique-constraint conflict instead of failing.
        select_portfolio = select(Portfolio).where(Portfolio.name == PORTFOLIO_NAME)
        portfolio = (await session.execute(select_portfolio)).scalar_one_or_none()
        if portfolio is None:
            try:
                async with session.begin_nested():
                    session.add(Portfolio(name=PORTFOLIO_NAME, base_currency="USD", cash_balance=Decimal("0")))
            except IntegrityError:
                pass
            portfolio = (await session.execute(select_portfolio)).scalar_one()
        exists = (await session.execute(select(RiskSettings.id).where(RiskSettings.portfolio_id == portfolio.id))).first()
        if exists is None:
            try:
                async with session.begin_nested():
                    session.add(
                        RiskSettings(portfolio_id=portfolio.id, **{k: Decimal(str(v)) for k, v in DEFAULT_RISK.items()})
                    )
            except IntegrityError:
                pass
        self._portfolio_id = portfolio.id
        return portfolio

    def _apply_risk(self) -> None:
        self._analysis.set_risk_params(
            replace(
                self._analysis.risk_params,
                max_risk_per_signal_pct=self.risk["max_risk_per_signal_pct"],
                max_allocation_pct=self.risk["max_allocation_per_opportunity_pct"],
                fee_pct=self.risk["fee_pct"],
                slippage_pct=self.risk["slippage_pct"],
            )
        )

    async def set_cash(self, cash: float) -> None:
        await self._ready()
        if not 0 <= cash < 1e13:
            raise PortfolioError("cash must be zero or positive")
        if self._sessions is not None:
            async with self._sessions() as session:
                portfolio = await self._ensure(session)
                portfolio.cash_balance = Decimal(str(round(cash, 8)))
                portfolio.updated_at = utcnow()
                await session.commit()
        self.cash = cash

    async def upsert_position(self, symbol: str, quantity: float, average_entry: float, notes: str | None) -> Position:
        await self._ready()
        symbol = WatchlistService.normalize(symbol)
        if not (0 < quantity < 1e15) or not (0 < average_entry < 1e12):
            raise PortfolioError("quantity and average entry must be positive")
        notes = (notes or "").strip()[:500] or None
        if self._sessions is not None:
            async with self._sessions() as session:
                portfolio = await self._ensure(session)
                row = (
                    await session.execute(
                        select(PortfolioPosition).where(
                            PortfolioPosition.portfolio_id == portfolio.id, PortfolioPosition.symbol == symbol
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = PortfolioPosition(portfolio_id=portfolio.id, symbol=symbol, quantity=Decimal(0), average_entry=Decimal(0))
                    session.add(row)
                row.quantity = Decimal(str(quantity))
                row.average_entry = Decimal(str(average_entry))
                row.notes = notes
                row.updated_at = utcnow()
                await session.commit()
        position = Position(symbol, quantity, average_entry, notes)
        self.positions[symbol] = position
        return position

    async def delete_position(self, symbol: str) -> bool:
        await self._ready()
        symbol = WatchlistService.normalize(symbol)
        if symbol not in self.positions:
            return False
        if self._sessions is not None:
            async with self._sessions() as session:
                portfolio = await self._ensure(session)
                await session.execute(
                    delete(PortfolioPosition).where(
                        PortfolioPosition.portfolio_id == portfolio.id, PortfolioPosition.symbol == symbol
                    )
                )
                await session.commit()
        del self.positions[symbol]
        return True

    async def update_risk(self, values: dict[str, float]) -> dict[str, float]:
        await self._ready()
        updated = dict(self.risk)
        for key, value in values.items():
            if key not in RISK_LIMITS or value is None:
                continue
            low, high = RISK_LIMITS[key]
            if not low <= float(value) <= high:
                raise PortfolioError(f"{key} must be between {low:g} and {high:g}")
            updated[key] = float(value)
        if updated["initial_allocation_pct"] > updated["max_allocation_per_opportunity_pct"]:
            raise PortfolioError("initial allocation cannot exceed the maximum allocation per opportunity")
        if self._sessions is not None:
            async with self._sessions() as session:
                portfolio = await self._ensure(session)
                row = (await session.execute(select(RiskSettings).where(RiskSettings.portfolio_id == portfolio.id))).scalar_one()
                for key, value in updated.items():
                    setattr(row, key, Decimal(str(value)))
                row.updated_at = utcnow()
                await session.commit()
        self.risk = updated
        self._apply_risk()
        return updated

    # ------------------------------------------------------------------ prices

    async def prices(self, symbols: list[str]) -> tuple[dict[str, float], dict[str, str], list[str]]:
        """Latest price per symbol and where it came from (live stream first, then a ticker call)."""
        prices: dict[str, float] = {}
        sources: dict[str, str] = {}
        errors: list[str] = []
        live = getattr(self._live, "live_prices", None)
        if callable(live):
            for symbol, quote in live().items():
                if symbol in symbols:
                    prices[symbol], sources[symbol] = quote["price"], "live stream"
        missing = [s for s in symbols if s not in prices]
        if not missing:
            return prices, sources, errors
        candidates: dict[str, list[Any]] = {}
        try:
            universe = await self._universe.get()
            for asset in universe.assets:
                if asset.symbol in missing and asset.markets:
                    candidates[asset.symbol] = asset.markets
        except Exception as exc:
            errors.append(f"universe unavailable: {exc}")
        unresolved = [s for s in missing if s not in candidates]
        if unresolved:
            resolved, resolve_errors = await self._router.resolve(unresolved)
            candidates.update({s: refs for s, refs in resolved.items() if refs})
            errors.extend(resolve_errors)
        if candidates:
            found, ticker_errors = await self._router.tickers(candidates)
            for symbol, (ticker, ref) in found.items():
                prices[symbol], sources[symbol] = ticker.last_price, f"{ref.adapter} {ref.symbol}"
            errors.extend(f"{s}: {'; '.join(e)}" for s, e in ticker_errors.items())
        errors.extend(f"{s}: no spot market found" for s in missing if s not in candidates)
        return prices, sources, errors

    # ------------------------------------------------------------------ views

    def _signal_for(self, symbol: str) -> Any:
        return self._analysis.cached(symbol)

    async def overview(self) -> dict[str, Any]:
        await self._ready()
        symbols = sorted(self.positions)
        prices, sources, errors = await self.prices(symbols) if symbols else ({}, {}, [])
        rows = []
        invested = 0.0
        for symbol in symbols:
            p = self.positions[symbol]
            price = prices.get(symbol)
            value = p.quantity * price if price is not None else None
            cost = p.quantity * p.average_entry
            if value is not None:
                invested += value
            analysis = self._signal_for(symbol)
            plan = analysis.plan if analysis else None
            scenarios = []
            if price is not None:
                for move in SCENARIO_MOVES:
                    target = price * (1 + move / 100)
                    scenarios.append({"label": f"{move:+g}%", "price": target, "pnl": p.quantity * (target - p.average_entry)})
                if plan is not None:
                    for label, level in [("signal stop", plan.stop_loss)] + [
                        (f"TP{i}", t.price) for i, t in enumerate(plan.targets, start=1)
                    ]:
                        scenarios.append({"label": label, "price": level, "pnl": p.quantity * (level - p.average_entry)})
            rows.append(
                {
                    "symbol": symbol,
                    "quantity": p.quantity,
                    "average_entry": p.average_entry,
                    "notes": p.notes,
                    "price": price,
                    "price_source": sources.get(symbol),
                    "cost": cost,
                    "value": value,
                    "pnl": value - cost if value is not None else None,
                    "pnl_pct": (value / cost - 1) * 100 if value is not None and cost > 0 else None,
                    "signal": analysis.signal.value if analysis else None,
                    "scenarios": scenarios,
                }
            )
        equity = self.cash + invested
        for row in rows:
            row["allocation_pct"] = row["value"] / equity * 100 if row["value"] is not None and equity > 0 else None
        exposure = invested / equity * 100 if equity > 0 else 0.0
        warnings = []
        if exposure > self.risk["max_total_open_allocation_pct"]:
            warnings.append(
                f"open positions are {exposure:.1f}% of equity, above your {self.risk['max_total_open_allocation_pct']:g}% limit"
            )
        for row in rows:
            if row["allocation_pct"] is not None and row["allocation_pct"] > self.risk["max_allocation_per_opportunity_pct"] * 1.5:
                warnings.append(f"{row['symbol']} is {row['allocation_pct']:.1f}% of equity (concentration risk)")
        return {
            "generated_at": utcnow(),
            "currency": "USD (USDT treated as USD)",
            "cash": self.cash,
            "invested": invested,
            "equity": equity,
            "exposure_pct": exposure,
            "unrealized_pnl": sum(r["pnl"] for r in rows if r["pnl"] is not None),
            "positions": rows,
            "risk_settings": dict(self.risk),
            "warnings": warnings,
            "errors": errors,
            "persistence": "ok" if self._sessions is not None else "disabled",
        }

    async def trade_plan(self, symbol: str, budget: float | None = None) -> dict[str, Any]:
        """Position size, DCA ladder and P/L scenarios for the current plan of `symbol`."""
        symbol = WatchlistService.normalize(symbol)
        analysis = await self._analysis.analyze(symbol)
        plan = analysis.plan
        if plan is None:
            reason = analysis.reasons[0] if analysis.reasons else "no setup"
            raise PortfolioError(f"{symbol} has no trade plan ({analysis.signal.value}: {reason})")
        overview = await self.overview()
        equity = overview["equity"]
        risk = self.risk
        warnings: list[str] = []
        if analysis.signal not in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
            warnings.append(f"{analysis.signal.value}: this is a watch plan, not a buy signal")
        suggested = equity * min(plan.suggested_allocation_pct, risk["max_allocation_per_opportunity_pct"]) / 100
        if budget is None:
            budget = suggested
            if budget <= 0:
                raise PortfolioError("set your cash balance (or enter a budget) to size the trade")
        if budget <= 0:
            raise PortfolioError("budget must be positive")
        if budget > self.cash and self.cash > 0:
            warnings.append(f"budget {budget:,.2f} is above your cash balance {self.cash:,.2f}")
        if equity > 0:
            room = equity * risk["max_total_open_allocation_pct"] / 100 - overview["invested"]
            if budget > room:
                warnings.append(f"this would exceed your {risk['max_total_open_allocation_pct']:g}% total exposure limit")

        initial, dca = risk["initial_allocation_pct"], risk["max_dca_allocation_pct"]
        first_share = initial / (initial + dca) if initial + dca > 0 else 1.0
        levels = [
            ("market / top of zone", plan.entry_high, first_share),
            ("middle of entry zone", (plan.entry_high + plan.entry_low) / 2, (1 - first_share) / 2),
            ("bottom of entry zone", plan.entry_low, (1 - first_share) / 2),
        ]
        tranches = [
            {"label": label, "price": price, "amount": budget * share, "quantity": budget * share / price}
            for label, price, share in levels
            if share > 0
        ]
        quantity = sum(t["quantity"] for t in tranches)
        average = budget / quantity
        cost_rate = (risk["fee_pct"] + risk["slippage_pct"]) / 100

        def outcome(exit_prices: list[tuple[float, float]]) -> float:
            """P/L after costs for exits given as (share of quantity, price)."""
            gross = sum(share * quantity * (price - average) for share, price in exit_prices)
            costs = budget * cost_rate + sum(share * quantity * price * cost_rate for share, price in exit_prices)
            return gross - costs

        t = plan.targets
        shares = [x.allocation_pct / 100 for x in t]
        scenarios = [
            {"label": "stop hit", "pnl": outcome([(1.0, plan.stop_loss)])},
            {"label": "TP1, then stop at entry", "pnl": outcome([(shares[0], t[0].price), (1 - shares[0], average)])},
            {"label": "TP1 + TP2, rest at entry", "pnl": outcome([(shares[0], t[0].price), (shares[1], t[1].price), (shares[2], average)])},
            {"label": "all targets", "pnl": outcome([(s, x.price) for s, x in zip(shares, t, strict=True)])},
        ]
        for scenario in scenarios:
            scenario["pnl_pct_of_equity"] = scenario["pnl"] / equity * 100 if equity > 0 else None
        loss = -scenarios[0]["pnl"]
        if equity > 0 and loss / equity * 100 > risk["max_risk_per_signal_pct"] * 1.05:
            warnings.append(
                f"a stop-out would lose {loss / equity * 100:.2f}% of equity, above your {risk['max_risk_per_signal_pct']:g}% limit"
            )
        return {
            "symbol": symbol,
            "signal": analysis.signal.value,
            "score": analysis.score,
            "quote_asset": plan.quote_asset,
            "equity": equity,
            "budget": budget,
            "suggested_budget": suggested,
            "tranches": tranches,
            "average_entry": average,
            "quantity": quantity,
            "stop_loss": plan.stop_loss,
            "targets": [{"price": x.price, "allocation_pct": x.allocation_pct, "basis": x.basis} for x in t],
            "loss_at_stop": loss,
            "loss_at_stop_pct_of_equity": loss / equity * 100 if equity > 0 else None,
            "scenarios": scenarios,
            "warnings": warnings,
            "note": "Tranches 2 and 3 only fill if price dips into the zone; if they do not, the position is smaller.",
        }

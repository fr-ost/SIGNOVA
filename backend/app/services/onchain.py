"""On-chain and whale activity from free public sources (Phase 5).

* Bitcoin network: mempool.space (fees, mempool, hashrate, difficulty adjustment).
* Large Bitcoin transactions: blockchain.com unconfirmed transactions (>= WHALE_MIN_BTC).
* Ethereum: Blockscout (gas, utilisation) and large ETH transfers (>= WHALE_MIN_ETH) with the
  address names Blockscout publishes, which identify many exchange wallets.
* Stablecoin supply: DefiLlama (total USD-pegged supply and its 1d/7d/30d change), a proxy
  for fresh liquidity entering or leaving crypto.
* Optional: Whale Alert (WHALE_ALERT_API_KEY) for labelled transfers on every major chain.

Everything is fetched on request and cached. A failing source is reported, never guessed.
Exchange inflow/outflow is only claimed when a source labels one side as an exchange.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.enums import ProviderStatus
from app.core.timeutil import parse_iso, utcnow
from app.data.health import ProviderHealthRegistry
from app.models import WhaleEvent
from app.services.cache import AsyncTTLCache

log = logging.getLogger(__name__)

EXCHANGE_WORDS = (
    "binance", "coinbase", "kraken", "okx", "okex", "bybit", "bitfinex", "huobi", "htx", "kucoin", "gemini",
    "bitstamp", "crypto.com", "gate.io", "bitget", "mexc", "upbit", "bithumb", "poloniex", "robinhood",
)
MAX_WHALES = 40


class SourceError(Exception):
    pass


async def fetch_json(
    http: httpx.AsyncClient,
    health: ProviderHealthRegistry,
    provider: str,
    role: str,
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = 12.0,
) -> Any:
    """GET JSON from a public source and record its health. Never includes the URL in errors."""
    health.register(provider, role)
    started = time.monotonic()
    try:
        response = await http.get(url, params=params, timeout=timeout, follow_redirects=True,
                                  headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        health.record_failure(provider, f"transport error: {type(exc).__name__}")
        raise SourceError(f"{provider}: unreachable ({type(exc).__name__})") from None
    if response.status_code >= 400:
        status = ProviderStatus.RESTRICTED if response.status_code in (401, 403, 451) else None
        health.record_failure(provider, f"HTTP {response.status_code}", status=status)
        raise SourceError(f"{provider}: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        health.record_failure(provider, "invalid JSON")
        raise SourceError(f"{provider}: invalid JSON") from None
    health.record_success(provider, (time.monotonic() - started) * 1000)
    return data


def _num(value: Any) -> float | None:
    if isinstance(value, dict):  # newer Blockscout shapes: {"price": 1.2, ...}
        value = value.get("price", value.get("value"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_transfer(from_label: str | None, to_label: str | None) -> str:
    def is_exchange(label: str | None) -> bool:
        return bool(label) and any(word in label.lower() for word in EXCHANGE_WORDS)

    src, dst = is_exchange(from_label), is_exchange(to_label)
    if dst and not src:
        return "exchange_inflow"  # coins moved onto an exchange: often ahead of selling
    if src and not dst:
        return "exchange_outflow"  # coins withdrawn from an exchange: often accumulation
    if src and dst:
        return "inter_exchange"
    return "unknown"


@dataclass
class Whale:
    uid: str
    source: str
    chain: str
    symbol: str
    tx_hash: str | None
    amount: float
    amount_usd: float | None
    occurred_at: datetime
    classification: str = "unknown"
    from_label: str | None = None
    to_label: str | None = None
    url: str | None = None


@dataclass
class OnChainDigest:
    fetched_at: datetime
    btc: dict[str, Any] = field(default_factory=dict)
    eth: dict[str, Any] = field(default_factory=dict)
    stablecoins: dict[str, Any] = field(default_factory=dict)
    whales: list[Whale] = field(default_factory=list)
    flows: dict[str, dict[str, float]] = field(default_factory=dict)  # symbol -> inflow/outflow USD
    sources_ok: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------------- parsers


def parse_mempool(fees: Any, mempool: Any, difficulty: Any, hashrate: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(fees, dict):
        out["fees_sat_vb"] = {k: fees.get(k) for k in ("fastestFee", "halfHourFee", "hourFee", "economyFee")}
    if isinstance(mempool, dict):
        out["mempool_tx_count"] = mempool.get("count")
        vsize = _num(mempool.get("vsize"))
        out["mempool_vsize_mb"] = round(vsize / 1e6, 2) if vsize is not None else None
    if isinstance(difficulty, dict):
        out["difficulty_change_pct"] = _num(difficulty.get("difficultyChange"))
        out["difficulty_progress_pct"] = _num(difficulty.get("progressPercent"))
        out["blocks_to_retarget"] = difficulty.get("remainingBlocks")
    if isinstance(hashrate, dict):
        current = _num(hashrate.get("currentHashrate"))
        out["hashrate_ehs"] = round(current / 1e18, 1) if current else None
    return out


def parse_btc_whales(payload: Any, min_btc: float, price: float | None) -> list[Whale]:
    txs = payload.get("txs") if isinstance(payload, dict) else None
    whales = []
    for tx in txs or []:
        if not isinstance(tx, dict) or not tx.get("hash"):
            continue
        total = sum(o.get("value", 0) for o in tx.get("out", []) if isinstance(o, dict) and isinstance(o.get("value"), int))
        amount = total / 1e8
        if amount < min_btc:
            continue
        stamp = tx.get("time")
        whales.append(
            Whale(
                uid=f"btc:{tx['hash']}",
                source="blockchain.com",
                chain="bitcoin",
                symbol="BTC",
                tx_hash=tx["hash"],
                amount=amount,
                amount_usd=amount * price if price else None,
                occurred_at=datetime.fromtimestamp(stamp, tz=UTC) if isinstance(stamp, int) else utcnow(),
                url=f"https://mempool.space/tx/{tx['hash']}",
            )
        )
    return whales


def _address_label(side: Any) -> str | None:
    if not isinstance(side, dict):
        return None
    name = side.get("name")
    if not name and isinstance(side.get("metadata"), dict):
        tags = side["metadata"].get("tags") or []
        name = next((t.get("name") for t in tags if isinstance(t, dict) and t.get("name")), None)
    return str(name)[:128] if name else None


def parse_eth_whales(payload: Any, min_eth: float, price: float | None) -> list[Whale]:
    items = payload.get("items") if isinstance(payload, dict) else payload
    whales = []
    for tx in items or []:
        if not isinstance(tx, dict) or not tx.get("hash"):
            continue
        value = _num(tx.get("value"))
        if value is None:
            continue
        amount = value / 1e18
        if amount < min_eth:
            continue
        from_label, to_label = _address_label(tx.get("from")), _address_label(tx.get("to"))
        whales.append(
            Whale(
                uid=f"eth:{tx['hash']}",
                source="blockscout",
                chain="ethereum",
                symbol="ETH",
                tx_hash=tx["hash"],
                amount=amount,
                amount_usd=amount * price if price else None,
                occurred_at=parse_iso(tx.get("timestamp")) or utcnow(),
                classification=classify_transfer(from_label, to_label),
                from_label=from_label,
                to_label=to_label,
                url=f"https://etherscan.io/tx/{tx['hash']}",
            )
        )
    return whales


def parse_whale_alert(payload: Any) -> list[Whale]:
    txs = payload.get("transactions") if isinstance(payload, dict) else None
    whales = []
    for tx in txs or []:
        if not isinstance(tx, dict) or not tx.get("hash"):
            continue
        frm, to = tx.get("from") or {}, tx.get("to") or {}
        from_label = frm.get("owner") or (frm.get("owner_type") if frm.get("owner_type") != "unknown" else None)
        to_label = to.get("owner") or (to.get("owner_type") if to.get("owner_type") != "unknown" else None)
        if frm.get("owner_type") == "exchange" and to.get("owner_type") != "exchange":
            cls = "exchange_outflow"
        elif to.get("owner_type") == "exchange" and frm.get("owner_type") != "exchange":
            cls = "exchange_inflow"
        elif frm.get("owner_type") == "exchange":
            cls = "inter_exchange"
        else:
            cls = "unknown"
        stamp = tx.get("timestamp")
        whales.append(
            Whale(
                uid=f"wa:{tx.get('blockchain')}:{tx['hash']}:{tx.get('id', '')}"[:160],
                source="whale-alert",
                chain=str(tx.get("blockchain", "")),
                symbol=str(tx.get("symbol", "")).upper(),
                tx_hash=str(tx["hash"])[:128],
                amount=_num(tx.get("amount")) or 0.0,
                amount_usd=_num(tx.get("amount_usd")),
                occurred_at=datetime.fromtimestamp(stamp, tz=UTC) if isinstance(stamp, int) else utcnow(),
                classification=cls,
                from_label=str(from_label)[:128] if from_label else None,
                to_label=str(to_label)[:128] if to_label else None,
            )
        )
    return whales


def parse_stablecoins(payload: Any) -> dict[str, Any]:
    assets = payload.get("peggedAssets") if isinstance(payload, dict) else None
    totals = {"now": 0.0, "day": 0.0, "week": 0.0, "month": 0.0}
    keys = {"now": "circulating", "day": "circulatingPrevDay", "week": "circulatingPrevWeek", "month": "circulatingPrevMonth"}
    counted = 0
    for asset in assets or []:
        if not isinstance(asset, dict) or asset.get("pegType") != "peggedUSD":
            continue
        values = {k: _num((asset.get(v) or {}).get("peggedUSD")) for k, v in keys.items()}
        if values["now"] is None:
            continue
        counted += 1
        for k, v in values.items():
            totals[k] += v if v is not None else values["now"]
    if not counted or totals["now"] <= 0:
        return {}

    def change(prev: float) -> float | None:
        return round((totals["now"] / prev - 1) * 100, 3) if prev > 0 else None

    return {
        "total_usd": totals["now"],
        "change_1d_pct": change(totals["day"]),
        "change_7d_pct": change(totals["week"]),
        "change_30d_pct": change(totals["month"]),
        "stablecoins_counted": counted,
    }


# ----------------------------------------------------------------------------- service


class OnChainService:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        prices_usd: Callable[[], dict[str, float]],
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._s = settings
        self._http = http
        self._health = health
        self._prices = prices_usd
        self._sessions = session_factory
        self._cache = AsyncTTLCache()

    def cached(self) -> OnChainDigest | None:
        entry = self._cache.peek("onchain")
        return entry[0] if entry else None

    async def digest(self, *, force: bool = False) -> OnChainDigest:
        entry = self._cache.peek("onchain")
        if force and entry is not None and entry[1] < self._s.min_refresh_seconds:
            force = False  # protect the free sources from rapid refreshes
        return await self._cache.get_or_load("onchain", self._load, self._s.onchain_cache_seconds, force=force)

    async def _get(self, provider: str, url: str, params: dict[str, Any] | None = None) -> Any:
        return await fetch_json(self._http, self._health, provider, "onchain", url, params)

    async def _load(self) -> OnChainDigest:
        s = self._s
        mp = s.mempool_base_url.rstrip("/")
        jobs: dict[str, Any] = {
            "fees": self._get("mempool_space", f"{mp}/v1/fees/recommended"),
            "mempool": self._get("mempool_space", f"{mp}/mempool"),
            "difficulty": self._get("mempool_space", f"{mp}/v1/difficulty-adjustment"),
            "hashrate": self._get("mempool_space", f"{mp}/v1/mining/hashrate/3d"),
            "btc_txs": self._get("blockchain_com", f"{s.blockchain_info_url.rstrip('/')}/unconfirmed-transactions", {"format": "json"}),
            "eth_stats": self._get("blockscout", f"{s.blockscout_eth_url.rstrip('/')}/stats"),
            "eth_txs": self._get("blockscout", f"{s.blockscout_eth_url.rstrip('/')}/main-page/transactions"),
            "stables": self._get("defillama", f"{s.defillama_stablecoins_url.rstrip('/')}/stablecoins", {"includePrices": "false"}),
        }
        key = s.whale_alert_key
        if key:
            start = int((utcnow() - timedelta(hours=1)).timestamp())
            jobs["whale_alert"] = self._get(
                "whale_alert", "https://api.whale-alert.io/v1/transactions",
                {"api_key": key, "min_value": s.whale_alert_min_usd, "start": start, "limit": 100},
            )
        results = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True), strict=True))
        errors = sorted({str(r) for r in results.values() if isinstance(r, BaseException)})
        ok = {k: v for k, v in results.items() if not isinstance(v, BaseException)}
        prices = self._prices()
        digest = OnChainDigest(fetched_at=utcnow(), errors=errors)
        digest.btc = parse_mempool(ok.get("fees"), ok.get("mempool"), ok.get("difficulty"), ok.get("hashrate"))
        stats = ok.get("eth_stats")
        if isinstance(stats, dict):
            gas = stats.get("gas_prices") if isinstance(stats.get("gas_prices"), dict) else {}
            digest.eth = {
                "gas_gwei": {k: _num(gas.get(k)) for k in ("slow", "average", "fast")},
                "transactions_today": _num(stats.get("transactions_today")),
                "network_utilization_pct": _num(stats.get("network_utilization_percentage")),
            }
        digest.stablecoins = parse_stablecoins(ok.get("stables"))
        whales: list[Whale] = []
        if "btc_txs" in ok:
            whales += parse_btc_whales(ok["btc_txs"], s.whale_min_btc, prices.get("BTC"))
        if "eth_txs" in ok:
            whales += parse_eth_whales(ok["eth_txs"], s.whale_min_eth, prices.get("ETH"))
        if "whale_alert" in ok:
            whales += parse_whale_alert(ok["whale_alert"])
        unique = {w.uid: w for w in whales}
        digest.whales = sorted(unique.values(), key=lambda w: w.amount_usd or 0.0, reverse=True)[:MAX_WHALES]
        for w in digest.whales:
            if w.amount_usd and w.classification in ("exchange_inflow", "exchange_outflow"):
                flow = digest.flows.setdefault(w.symbol, {"exchange_inflow": 0.0, "exchange_outflow": 0.0})
                flow[w.classification] += w.amount_usd
        digest.sources_ok = sorted({
            name for name, parts in {
                "mempool.space": ("fees", "mempool", "difficulty", "hashrate"), "blockchain.com": ("btc_txs",),
                "blockscout": ("eth_stats", "eth_txs"), "defillama": ("stables",), "whale-alert": ("whale_alert",),
            }.items() if any(p in ok for p in parts)
        })
        await self._persist(digest.whales)
        return digest

    async def _persist(self, whales: list[Whale]) -> None:
        if self._sessions is None or not whales:
            return
        from app.services.persistence import _insert

        now = utcnow()
        rows = [
            {
                "event_uid": w.uid, "source": w.source, "chain": w.chain, "symbol": w.symbol, "tx_hash": w.tx_hash,
                "event_type": "transfer", "classification": w.classification, "amount": w.amount,
                "amount_usd": w.amount_usd, "from_label": w.from_label, "to_label": w.to_label,
                "occurred_at": w.occurred_at, "collected_at": now, "details": {"url": w.url},
            }
            for w in whales
        ]
        try:
            async with self._sessions() as session:
                await session.execute(_insert(session, WhaleEvent).values(rows).on_conflict_do_nothing())
                await session.commit()
        except Exception:
            log.exception("whale event persistence failed")

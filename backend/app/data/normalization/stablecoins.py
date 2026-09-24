"""Asset classification used to build the Top-20 universe.

Stablecoins are always excluded (spec requirement). Wrapped, bridged, liquid-staking and
commodity-pegged tokens are excluded by default (EXCLUDE_WRAPPED_ASSETS=true) because
their price only mirrors another asset, which makes separate spot signals meaningless.

Detection combines three independent signals so a single missing tag cannot let a
stablecoin slip into the universe: provider tags, a curated symbol list, and a USD-peg
price heuristic for symbols that look like dollar tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data.normalization.schemas import ListingEntry

KNOWN_STABLECOINS = frozenset(
    {
        "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USD1", "RLUSD",
        "USDD", "FRAX", "BUSD", "USDP", "GUSD", "LUSD", "CRVUSD", "GHO", "USDX", "USDB",
        "USDY", "USD0", "USDG", "USDF", "USTB", "BSC-USD", "SUSD", "EURC", "EURT", "EURS",
        "AEUR", "EURI", "USDA", "DOLA", "MIM", "ALUSD", "FXUSD", "USR", "DEUSD", "BUIDL",
    }
)

KNOWN_WRAPPED_OR_DERIVATIVE = frozenset(
    {
        "WBTC", "WETH", "STETH", "WSTETH", "WBETH", "WEETH", "EETH", "CBBTC", "CBETH", "RETH",
        "METH", "EZETH", "RSETH", "LBTC", "SOLVBTC", "BTCB", "JITOSOL", "MSOL", "BNSOL",
        "JUPSOL", "SUSDE", "SUSDS", "TBTC", "CLBTC", "WTRX", "WBNB", "WSOL", "STSOL",
        "OSETH", "SWETH", "PUFETH", "SFRXETH", "FRXETH", "UNIBTC", "PUMPBTC", "ENZOBTC",
        "XAUT", "PAXG", "KAU", "KAG",
    }
)

_STABLE_TAG_MARKERS = ("stablecoin",)
_WRAPPED_TAG_MARKERS = (
    "wrapped-tokens",
    "wrapped",
    "bridged-tokens",
    "liquid-staking-derivatives",
    "liquid-staking-tokens",
    "liquid-restaking-tokens",
    "liquid-staked",
    "tokenized-gold",
    "tokenized-commodities",
    "tokenized-treasury",
)
_WRAPPED_NAME_MARKERS = ("wrapped ", "bridged ", "staked ", "restaked ")
_USD_PEG_BAND = (0.95, 1.05)


@dataclass(frozen=True, slots=True)
class AssetClassification:
    is_stablecoin: bool
    is_wrapped_or_derivative: bool
    reason: str | None


def classify_listing(entry: ListingEntry) -> AssetClassification:
    symbol = entry.symbol.upper()
    tags = tuple(tag.lower() for tag in entry.tags)
    name = entry.name.lower()

    if symbol in KNOWN_STABLECOINS:
        return AssetClassification(True, False, "known stablecoin")
    if any(marker in tag for tag in tags for marker in _STABLE_TAG_MARKERS):
        return AssetClassification(True, False, "tagged stablecoin by listing source")
    looks_like_dollar = "USD" in symbol or "usd" in name
    if looks_like_dollar and _USD_PEG_BAND[0] <= entry.price_usd <= _USD_PEG_BAND[1]:
        return AssetClassification(True, False, "USD-named token trading at the $1 peg")

    if symbol in KNOWN_WRAPPED_OR_DERIVATIVE:
        return AssetClassification(False, True, "known wrapped/staked/pegged token")
    if any(marker in tag for tag in tags for marker in _WRAPPED_TAG_MARKERS):
        return AssetClassification(False, True, "tagged wrapped/staked/pegged by listing source")
    if any(name.startswith(marker) or f" {marker}" in f" {name}" for marker in _WRAPPED_NAME_MARKERS):
        return AssetClassification(False, True, "name indicates wrapped/staked token")

    return AssetClassification(False, False, None)

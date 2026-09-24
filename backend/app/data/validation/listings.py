"""Validation of market-cap listings before they are allowed to define the universe."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.data.normalization.schemas import ListingEntry


@dataclass
class ListingValidation:
    ok: bool
    entries: list[ListingEntry]
    newest_update_age_seconds: float | None
    issues: list[str] = field(default_factory=list)


def validate_listing(
    entries: Sequence[ListingEntry],
    *,
    min_entries: int,
    now: datetime,
    max_age_seconds: float,
) -> ListingValidation:
    issues: list[str] = []
    cleaned: dict[str, ListingEntry] = {}
    dropped = 0
    for entry in entries:
        if not entry.symbol or entry.price_usd <= 0 or entry.market_cap_usd <= 0:
            dropped += 1
            continue
        existing = cleaned.get(entry.symbol)
        # Duplicate tickers exist across projects; keep the larger market cap.
        if existing is None or entry.market_cap_usd > existing.market_cap_usd:
            cleaned[entry.symbol] = entry
    if dropped:
        issues.append(f"{dropped} listing rows with invalid price or market cap dropped")
    ordered = sorted(cleaned.values(), key=lambda e: e.market_cap_usd, reverse=True)

    timestamps = [e.last_updated for e in ordered if e.last_updated is not None]
    newest_age = (now - max(timestamps)).total_seconds() if timestamps else None
    ok = True
    if len(ordered) < min_entries:
        ok = False
        issues.append(f"only {len(ordered)} valid listings; {min_entries} required")
    if newest_age is None:
        issues.append("listing has no update timestamps; prices cannot be treated as current")
    elif newest_age > max_age_seconds:
        ok = False
        issues.append(f"listing data is {newest_age:.0f}s old")
    return ListingValidation(ok=ok, entries=ordered, newest_update_age_seconds=newest_age, issues=issues)

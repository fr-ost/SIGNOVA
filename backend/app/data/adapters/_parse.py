"""Small, strict parsing helpers shared by adapters."""

from __future__ import annotations

import math
from typing import Any

from app.data.http import ProviderBadResponse


def to_float(value: Any, provider: str, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProviderBadResponse(provider, f"field {field!r} is not numeric: {value!r}") from None
    if not math.isfinite(number):
        raise ProviderBadResponse(provider, f"field {field!r} is not finite")
    return number


def opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

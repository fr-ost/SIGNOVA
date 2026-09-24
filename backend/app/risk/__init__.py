"""Phase 2 risk engine: trade plan (entry, stop, targets, sizing) and risk checks.

The risk engine sits above the quantitative score: a failed check can block a signal
(NO TRADE), cap it (at most WATCH) or downgrade it (at most BUY). Nothing downstream,
including the later AI layer, can lift those limits.
"""

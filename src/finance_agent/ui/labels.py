"""Data-origin labelling (pure functions): synthetic data must never look like a live finding."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OriginLabel:
    text: str
    kind: str  # success | info | error | warning (maps to st.success/info/error/warning)
    synthetic: bool


LIVE = OriginLabel("LIVE — retrieved from the official source", "success", False)
REAL_SNAPSHOT = OriginLabel(
    "HISTORICAL REAL SNAPSHOT — recorded official data (offline fixture)", "info", False
)
SYNTHETIC = OriginLabel("SYNTHETIC FIXTURE — invented test data, NOT real findings", "error", True)
UNKNOWN = OriginLabel("MISSING / UNVERIFIED ORIGIN — provenance not established", "warning", False)


def _one(origin: str) -> OriginLabel:
    o = origin.strip().lower()
    if o == "live":
        return LIVE
    if o == "fixture:real_snapshot":
        return REAL_SNAPSHOT
    if "synthetic" in o:
        return SYNTHETIC
    return UNKNOWN


def origin_label(origins: str | list[str] | None) -> OriginLabel:
    """One label for a response. ``mixed:a,b`` and lists are reduced to the most cautious member:
    synthetic > unknown > real snapshot > live."""
    if not origins:
        return UNKNOWN
    if isinstance(origins, str):
        parts = origins.removeprefix("mixed:").split(",")
    else:
        parts = [p for o in origins for p in o.removeprefix("mixed:").split(",")]
    labels = {_one(p) for p in parts if p.strip()} or {UNKNOWN}
    for worst in (SYNTHETIC, UNKNOWN, REAL_SNAPSHOT, LIVE):
        if worst in labels:
            if len(labels) > 1 and worst is not LIVE:
                return OriginLabel(f"MIXED ORIGINS — {worst.text}", worst.kind, worst.synthetic)
            return worst
    return UNKNOWN


def verification_kind(status: str | None) -> str:
    return {"passed": "success", "passed_with_warnings": "warning", "failed": "error"}.get(
        status or "", "warning"
    )

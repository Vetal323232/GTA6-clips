"""Highlight-quality evaluation records, kept separate from technical checks.

Matching rule for manually marked intervals: a marked interval counts as
covered when at least one candidate of the latest analysis overlaps it by
>= 50 % of the marked interval's length, or contains its midpoint, with a
1.0 s tolerance on both candidate edges. Review decisions are only recorded;
they are never used to retrain or re-weight anything automatically.
"""

from __future__ import annotations

import json

TOLERANCE = 1.0
MIN_OVERLAP = 0.5


def covered(mark: dict, cand: dict) -> bool:
    s, e = cand["start"] - TOLERANCE, cand["end"] + TOLERANCE
    length = mark["end"] - mark["start"]
    overlap = max(0.0, min(e, mark["end"]) - max(s, mark["start"]))
    mid = (mark["start"] + mark["end"]) / 2
    return overlap >= MIN_OVERLAP * length or s <= mid <= e


def evaluate(candidates: list[dict], marks: list[dict], jobs: list[dict], ks=(1, 3, 5)) -> dict:
    by_rank = sorted(candidates, key=lambda c: c["rank"])
    reviewed = [c for c in by_rank if c["status"] in ("accepted", "rejected")]
    report = {
        "rule": f"покрытие: пересечение ≥{int(MIN_OVERLAP * 100)}% отмеченного интервала или середина внутри, допуск ±{TOLERANCE} с",
        "total": len(by_rank),
        "reviewed": len(reviewed),
        "accepted": sum(c["status"] == "accepted" for c in by_rank),
        "rejected": sum(c["status"] == "rejected" for c in by_rank),
        "pending": sum(c["status"] == "pending" for c in by_rank),
        "top_k": [],
        "marks_total": len(marks),
        "marks_missed": [],
        "boundary_notes": [],
        "jobs": [],
    }
    for k in ks:
        top = by_rank[:k]
        if not top:
            continue
        rev = [c for c in top if c["status"] in ("accepted", "rejected")]
        report["top_k"].append({"k": k, "candidates": len(top), "reviewed": len(rev),
                                "accepted": sum(c["status"] == "accepted" for c in rev)})
    for m in marks:
        if not any(covered(m, c) for c in by_rank):
            report["marks_missed"].append({"start": m["start"], "end": m["end"], "note": m.get("note")})
    for c in by_rank:
        if c.get("reason") or c.get("edited"):
            report["boundary_notes"].append({
                "rank": c["rank"], "status": c["status"], "reason": c.get("reason"),
                "edited": bool(c.get("edited")),
                "auto": [c["auto_start"], c["auto_end"]], "final": [c["start"], c["end"]],
            })
    for j in jobs:
        signals = json.loads(j["signals"]) if j.get("signals") else {}
        report["jobs"].append({
            "job_id": j["id"], "elapsed_s": j.get("elapsed"), "peak_rss_mb": j.get("peak_rss_mb"),
            "signals_enabled": sorted(k for k, v in signals.items() if v.get("available")),
            "speech": signals.get("speech", {}).get("info"),
            "hardware": json.loads(j["hardware"]) if j.get("hardware") else None,
        })
    return report

"""Clip range planning with explicit invariants.

Every automatically created or adjusted range satisfies:
  0 <= start < end <= duration, end - start <= max_len, start <= anchor <= end,
and ranges of one plan never overlap. `check_range` is re-applied after every
merge or boundary move; a candidate that can't satisfy it is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-6


@dataclass
class Range:
    start: float
    end: float
    anchor: float
    score: float = 0.0
    extent: tuple[float, float] | None = None  # loud stretch around the anchor
    merged_anchors: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def check_range(r: Range, duration: float, max_len: float) -> bool:
    return (
        -EPS <= r.start < r.end <= duration + EPS
        and r.end - r.start <= max_len + EPS
        and r.start - EPS <= r.anchor <= r.end + EPS
    )


def initial_range(anchor: float, extent: tuple[float, float], duration: float,
                  before: float, after: float, max_len: float) -> tuple[float, float]:
    """Pad the event extent, clamp to the source and fit into max_len around the anchor."""
    start = max(0.0, extent[0] - before)
    end = min(duration, extent[1] + after)
    start, end = fit_around(anchor, start, end, duration, max_len)
    return start, end


def fit_around(anchor: float, start: float, end: float, duration: float, max_len: float) -> tuple[float, float]:
    """Shrink [start, end] to max_len keeping the anchor inside, preferring setup before it."""
    start, end = max(0.0, start), min(duration, end)
    if end - start <= max_len:
        return start, end
    # keep the setup:aftermath ratio of the requested padding, anchor inside
    before = anchor - start
    after = end - anchor
    total = before + after
    keep_before = max_len * (before / total) if total > 0 else max_len / 2
    s = max(start, anchor - keep_before)
    e = min(end, s + max_len)
    s = max(start, e - max_len)
    return s, e


def _contains(segments, t: float):
    for s, e in segments:
        if s < t < e:
            return s, e
    return None


def adjust_boundaries(r: Range, duration: float, max_len: float,
                      speech: list[tuple[float, float]] | None,
                      scene_cuts: list[float] | None) -> Range:
    """Avoid cutting through speech; optionally snap to a nearby scene cut.

    Speech boundaries win over scene cuts: a scene cut is only used when it
    doesn't move a boundary into the middle of speech.
    """
    start, end = r.start, r.end
    if speech:
        seg = _contains(speech, start)
        if seg and end - seg[0] <= max_len:
            start = max(0.0, seg[0])
            r.notes.append("начало сдвинуто к началу фразы")
        seg = _contains(speech, end)
        if seg and min(duration, seg[1]) - start <= max_len:
            end = min(duration, seg[1])
            r.notes.append("конец сдвинут к концу фразы")
    if scene_cuts:
        near = [c for c in scene_cuts if start - 1.5 <= c <= start + 1.0 and c < r.anchor - 1.0]
        if near:
            c = min(near, key=lambda c: abs(c - start))
            if not (speech and _contains(speech, c)) and end - c <= max_len and c < end:
                start = c
                r.notes.append("начало совмещено со сменой сцены")
    candidate = Range(start, end, r.anchor, r.score, r.extent, r.merged_anchors, r.notes)
    if check_range(candidate, duration, max_len):
        return candidate
    r.notes.append("корректировка границ отменена: нарушала ограничения")
    return r


def resolve_overlaps(ranges: list[Range], duration: float, max_len: float, min_clip: float) -> list[Range]:
    """Highest score first; merge an overlapping lower range only if the union fits, else trim or drop it."""
    accepted: list[Range] = []
    for r in sorted(ranges, key=lambda r: r.score, reverse=True):
        cur = Range(r.start, r.end, r.anchor, r.score, r.extent, list(r.merged_anchors), list(r.notes))
        changed = True
        while changed and cur is not None:
            changed = False
            for a in accepted:
                if cur.start < a.end - EPS and a.start < cur.end - EPS:
                    union = Range(min(a.start, cur.start), max(a.end, cur.end), a.anchor, a.score,
                                  a.extent, a.merged_anchors + [cur.anchor], a.notes)
                    clash = any(o is not a and union.start < o.end - EPS and o.start < union.end - EPS
                                for o in accepted)
                    if not clash and union.duration <= max_len + EPS and check_range(union, duration, max_len):
                        a.start, a.end, a.merged_anchors = union.start, union.end, union.merged_anchors
                        a.notes.append("объединено с соседним моментом")
                        cur = None
                        break
                    # trim cur away from a, keeping cur's anchor
                    if a.start - EPS <= cur.anchor <= a.end + EPS:
                        cur = None  # anchor already inside a stronger clip
                        break
                    if cur.anchor < a.start:
                        cur.end = min(cur.end, a.start)
                    else:
                        cur.start = max(cur.start, a.end)
                    cur.notes.append("обрезано, чтобы не пересекаться с более сильным моментом")
                    if cur.duration < min_clip or not check_range(cur, duration, max_len):
                        cur = None
                        break
                    changed = True
        if cur is not None and check_range(cur, duration, max_len):
            accepted.append(cur)
    for a in accepted:
        assert check_range(a, duration, max_len), a
    return sorted(accepted, key=lambda r: r.start)


def manual_edit(start: float, end: float, anchor: float, duration: float, max_len: float) -> tuple[float, float, list[str]]:
    """Validate a user edit. Hard limits are only the source bounds; the rest is flagged, not undone."""
    if not (0 <= start < end <= duration + EPS):
        raise ValueError(f"Начало и конец должны быть в пределах 0–{duration:.2f} с, начало раньше конца")
    flags = []
    if end - start > max_len + EPS:
        flags.append(f"длиннее максимума ({end - start:.1f} с > {max_len:.0f} с)")
    if not (start - EPS <= anchor <= end + EPS):
        flags.append("ключевой момент вне выбранного диапазона")
    return start, min(end, duration), flags

"""Heuristic, configurable candidate scoring.

Audio peaks are *candidate triggers*, not proof that a moment is interesting.
Each window gets normalized (0..1) contributions from every available signal;
the window score is their weighted sum (weights in config [weights]). A
candidate starts only where a trigger fires (global loudness, local loudness,
sudden energy rise, transcript cue or a HUD event). Speech and scene changes
only add supporting evidence. Unavailable signals contribute nothing and are
listed as unavailable in the explanation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .ranges import Range, adjust_boundaries, initial_range, resolve_overlaps
from .signals import SILENCE_DB, Features

DETECTOR_VERSION = "m1.2"

SIGNAL_LABELS = {
    "loud_global": "громкость относительно всей записи",
    "loud_rolling": "громкость относительно локального фона",
    "onset": "резкий рост громкости",
    "speech": "речь (VAD/распознавание)",
    "cue": "ключевая фраза в речи",
    "visual": "смена сцены / активность изображения",
    "hud": "событие HUD",
}


@dataclass
class Cue:
    time: float
    phrase: str
    text: str


@dataclass
class HudEvent:
    time: float
    label: str
    confidence: float = 1.0


@dataclass
class Candidate:
    rng: Range
    contributions: dict
    explanation: str
    triggers: list[str] = field(default_factory=list)


def fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


def find_cues(segments, cues: dict[str, list[str]]) -> list[Cue]:
    """Case-insensitive whole-phrase match of configured cues in transcript segments."""
    found = []
    patterns = []
    for words in cues.values():
        for phrase in words:
            p = phrase.strip().lower()
            if p:
                patterns.append((p, re.compile(r"(?<!\w)" + re.escape(p) + r"(?!\w)")))
    for seg in segments:
        text = seg.text.lower().replace("ё", "е")
        for phrase, rx in patterns:
            if rx.search(text) or rx.search(text.replace("е", "ё")):
                found.append(Cue(seg.start, phrase, seg.text))
    return found


def rolling_median(x: np.ndarray, width: int) -> np.ndarray:
    width = max(3, width | 1)
    pad = width // 2
    padded = np.pad(x, pad, mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.median(view, axis=1)


def window_signals(f: Features, cfg: dict, transcript_segments=None, cues: list[Cue] | None = None,
                   hud_events: list[HudEvent] | None = None) -> dict[str, np.ndarray | None]:
    """Raw and normalized per-window signals. None = unavailable."""
    a = cfg["analysis"]
    n = f.n
    out: dict[str, np.ndarray | None] = {}
    db = None
    if f.rms_db is not None and len(f.rms_db):
        db = np.full(n, SILENCE_DB, np.float32)
        db[: min(n, len(f.rms_db))] = f.rms_db[:n]
    out["db"] = db
    if db is not None:
        audible = db[db > -70]
        if len(audible) >= 4:
            med = float(np.median(audible))
            sd = float(np.std(audible)) or 1.0
            z = (db - med) / sd
            out["z_global"] = np.where(db > -70, z, -10).astype(np.float32)
            w = int(a["rolling_window"] / f.window)
            rmed = rolling_median(db, w)
            mad = rolling_median(np.abs(db - rmed), w)
            robust_sd = np.maximum(1.4826 * mad, 1.5)
            out["excess_db"] = (db - rmed).astype(np.float32)
            out["z_rolling"] = ((db - rmed) / robust_sd).astype(np.float32)
            k = max(1, int(2.0 / f.window))
            prev = np.convolve(np.pad(db, (k, 0), mode="edge"), np.ones(k) / k, mode="valid")[:-1][:n]
            out["jump_db"] = np.where(db > -70, db - prev, 0).astype(np.float32)
        else:
            out["z_global"] = out["z_rolling"] = out["jump_db"] = out["excess_db"] = None
    else:
        out["z_global"] = out["z_rolling"] = out["jump_db"] = out["excess_db"] = None

    speech_segments = list(f.speech_segments or [])
    if transcript_segments:
        speech_segments += [(s.start, s.end) for s in transcript_segments]
    if f.speech_segments is not None or transcript_segments:
        sp = np.zeros(n, np.float32)
        for s, e in speech_segments:
            i0, i1 = int(s / f.window), int(np.ceil(e / f.window))
            for i in range(max(0, i0), min(n, i1)):
                ws, we = i * f.window, (i + 1) * f.window
                sp[i] = max(sp[i], max(0.0, min(we, e) - max(ws, s)) / f.window)
        out["speech"] = sp
    else:
        out["speech"] = None

    if cues is not None:
        c = np.zeros(n, np.float32)
        for cue in cues:
            i = int(cue.time / f.window)
            c[max(0, i - 2): min(n, i + 3)] = 1.0
        out["cue"] = c
    else:
        out["cue"] = None

    if f.scene is not None:
        sc = np.zeros(n, np.float32)
        sc[: min(n, len(f.scene))] = f.scene[:n]
        out["scene"] = sc
    else:
        out["scene"] = None

    if hud_events is not None:
        h = np.zeros(n, np.float32)
        for ev in hud_events:
            i = int(ev.time / f.window)
            if 0 <= i < n:
                h[i] = max(h[i], float(ev.confidence))
        out["hud"] = h
    else:
        out["hud"] = None
    return out


def normalized(sig: dict, cfg: dict) -> dict[str, np.ndarray | None]:
    a = cfg["analysis"]
    clip = lambda x: np.clip(x, 0, 1)  # noqa: E731
    return {
        "loud_global": None if sig["z_global"] is None else clip(sig["z_global"] / (2 * a["sensitivity"])),
        "loud_rolling": None if sig["z_rolling"] is None else clip(sig["z_rolling"] / (2 * a["rolling_threshold"])),
        "onset": None if sig["jump_db"] is None else clip(sig["jump_db"] / (2 * a["onset_db"])),
        "speech": sig["speech"],
        "cue": sig["cue"],
        "visual": None if sig["scene"] is None else clip(sig["scene"] / 0.4),
        "hud": sig["hud"],
    }


def detect(f: Features, cfg: dict, transcript_segments=None, cues: list[Cue] | None = None,
           hud_events: list[HudEvent] | None = None, unavailable: dict[str, str] | None = None) -> list[Candidate]:
    a, weights = cfg["analysis"], cfg["weights"]
    sig = window_signals(f, cfg, transcript_segments, cues, hud_events)
    norm = normalized(sig, cfg)
    n = f.n
    score = np.zeros(n, np.float32)
    for k, arr in norm.items():
        if arr is not None:
            score += float(weights.get(k, 0)) * arr

    trig = np.zeros(n, bool)
    reasons: dict[int, list[str]] = {}

    def mark(mask, label):
        for i in np.flatnonzero(mask):
            trig[i] = True
            reasons.setdefault(int(i), []).append(label)

    if sig["z_global"] is not None:
        mark(sig["z_global"] >= a["sensitivity"], "loud_global")
        mark((sig["z_rolling"] >= a["rolling_threshold"]) & (sig["excess_db"] >= 6), "loud_rolling")
        mark(sig["jump_db"] >= a["onset_db"], "onset")
    if sig["cue"] is not None:
        mark(sig["cue"] > 0, "cue")
    if sig["hud"] is not None:
        mark(sig["hud"] > 0, "hud")

    groups: list[list[int]] = []
    gap = int(a["merge_gap"] / f.window)
    for i in np.flatnonzero(trig):
        if groups and i - groups[-1][-1] <= gap:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])

    duration, max_len = f.duration, a["max_len"]
    scene_cuts = None
    if sig["scene"] is not None and a.get("scene_boundaries", True):
        scene_cuts = [i * f.window for i in np.flatnonzero(sig["scene"] >= 0.3)]
    speech_bounds = None
    if a.get("speech_boundaries", True):
        segs = list(f.speech_segments or []) + [(s.start, s.end) for s in (transcript_segments or [])]
        speech_bounds = sorted(segs) or None

    ranges, meta = [], {}
    for g in groups:
        idx = g[int(np.argmax(score[g]))]
        anchor = min(duration, (idx + 0.5) * f.window)
        extent = (g[0] * f.window, min(duration, (g[-1] + 1) * f.window))
        s, e = initial_range(anchor, extent, duration, a["before"], a["after"], max_len,
                             float(a.get("target_min", 0)))
        r = Range(s, e, anchor, float(score[idx]) + 0.02 * min(10, len(g)), extent)
        r = adjust_boundaries(r, duration, max_len, speech_bounds, scene_cuts)
        ranges.append(r)
        meta[id(r)] = (idx, sorted({x for i in g for x in reasons.get(i, [])}))

    by_anchor = {r.anchor: meta[id(r)] for r in ranges}
    planned = resolve_overlaps(ranges, duration, max_len, a.get("min_clip", 3.0))
    planned = sorted(planned, key=lambda r: r.score, reverse=True)[: int(a["candidates_per_source"])]

    out = []
    for r in planned:
        idx, trig_labels = by_anchor[r.anchor]
        contrib = {}
        for k, arr in norm.items():
            if arr is None:
                contrib[k] = {"available": False, "reason": (unavailable or {}).get(k, "сигнал недоступен")}
            else:
                lo, hi = int(r.start / f.window), max(int(r.start / f.window) + 1, int(np.ceil(r.end / f.window)))
                contrib[k] = {"available": True, "value": round(float(arr[idx]), 3),
                              "weighted": round(float(arr[idx]) * float(weights.get(k, 0)), 3),
                              "max_in_clip": round(float(np.max(arr[lo:hi])), 3)}
        raw = {
            "db": _val(sig["db"], idx), "z_global": _val(sig["z_global"], idx),
            "excess_db": _val(sig["excess_db"], idx), "jump_db": _val(sig["jump_db"], idx),
            "scene": _val(sig["scene"], idx),
        }
        clip_cues = [c for c in (cues or []) if r.start <= c.time <= r.end]
        clip_hud = [h for h in (hud_events or []) if r.start <= h.time <= r.end]
        contrib["_raw"] = raw
        contrib["_triggers"] = trig_labels
        contrib["_cues"] = [{"time": c.time, "phrase": c.phrase} for c in clip_cues]
        contrib["_hud"] = [{"time": h.time, "label": h.label} for h in clip_hud]
        contrib["_notes"] = r.notes
        out.append(Candidate(r, contrib, explain(r, raw, trig_labels, contrib, clip_cues, clip_hud), trig_labels))
    return out


def _val(arr, i):
    return None if arr is None else round(float(arr[i]), 2)


def explain(r: Range, raw: dict, triggers: list[str], contrib: dict, cues: list[Cue], hud: list[HudEvent]) -> str:
    parts = [f"Ключевой момент {fmt_t(r.anchor)}, клип {fmt_t(r.start)}–{fmt_t(r.end)} ({r.duration:.1f} с)."]
    why = []
    if "loud_global" in triggers and raw["z_global"] is not None:
        why.append(f"громкость {raw['db']:.1f} дБFS — на {raw['z_global']:.1f} ст. откл. выше обычной для записи")
    if "loud_rolling" in triggers and raw["excess_db"] is not None:
        why.append(f"на {raw['excess_db']:.1f} дБ громче локального фона")
    if "onset" in triggers and raw["jump_db"] is not None:
        why.append(f"резкий рост громкости на {raw['jump_db']:.1f} дБ")
    for c in cues[:3]:
        why.append(f"фраза «{c.phrase}» в {fmt_t(c.time)}")
    for h in hud[:3]:
        why.append(f"событие HUD «{h.label}» в {fmt_t(h.time)}")
    if why:
        parts.append("Сработало: " + "; ".join(why) + ".")
    support = []
    if contrib.get("visual", {}).get("available") and (raw.get("scene") or 0) >= 0.1:
        support.append(f"смена сцены {raw['scene']:.2f}")
    if contrib.get("speech", {}).get("available") and contrib["speech"].get("max_in_clip", 0) > 0.3:
        support.append("в клипе есть речь")
    if support:
        parts.append("Дополнительно: " + ", ".join(support) + ".")
    missing = [SIGNAL_LABELS[k] for k, v in contrib.items() if not k.startswith("_") and not v.get("available")]
    if missing:
        parts.append("Не учитывалось (недоступно): " + ", ".join(missing) + ".")
    if r.notes:
        parts.append("Границы: " + "; ".join(dict.fromkeys(r.notes)) + ".")
    parts.append("Громкий звук — лишь признак, а не доказательство интересного момента.")
    return " ".join(parts)

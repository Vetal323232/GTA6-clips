"""Subtitle cue clipping, rebasing and SRT output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Cue:
    start: float
    end: float
    text: str


def clip_cues(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """Keep cues intersecting [start, end), cut them to it and rebase to the clip start.

    Guarantees 0 <= cue.start < cue.end <= end - start, source order preserved.
    """
    length = end - start
    out = []
    for c in sorted(cues, key=lambda c: (c.start, c.end)):
        s, e = max(c.start, start), min(c.end, end)
        if e - s <= 0.001:
            continue
        rs, re_ = round(s - start, 3), round(e - start, 3)
        rs, re_ = max(0.0, rs), min(round(length, 3), re_)
        if re_ > rs and c.text.strip():
            out.append(Cue(rs, re_, c.text.strip()))
    return out


def srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = []
    for i, c in enumerate(cues, 1):
        text = "\n".join(line for line in c.text.replace("\r", "").split("\n") if line.strip()) or " "
        blocks.append(f"{i}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{text}\n")
    return "\n".join(blocks)


def parse_srt(text: str) -> list[Cue]:
    cues = []
    for block in text.replace("\r", "").strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, b = [x.strip() for x in lines[1].split("-->")]
        cues.append(Cue(_parse_t(a), _parse_t(b), "\n".join(lines[2:])))
    return cues


def _parse_t(s: str) -> float:
    hms, ms = s.split(",")
    h, m, sec = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000

"""Minimal SRT parser + cut-boundary checks (for `makeacut check`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .timecode import parse_srt_tc

_TIME_LINE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


@dataclass
class Cue:
    index: int
    start_sec: float
    end_sec: float
    text: str


def parse_srt(path: str) -> List[Cue]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = fh.read()

    cues: List[Cue] = []
    # Split on blank lines between cue blocks (tolerant of CRLF).
    for block in re.split(r"\r?\n\r?\n", raw.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        time_idx = 0
        idx = 0
        if not _TIME_LINE.search(lines[0]) and len(lines) > 1:
            # first line is the numeric index
            try:
                idx = int(lines[0].strip())
            except ValueError:
                idx = 0
            time_idx = 1
        if time_idx >= len(lines):
            continue
        m = _TIME_LINE.search(lines[time_idx])
        if not m:
            continue
        start = parse_srt_tc(m.group(1))
        end = parse_srt_tc(m.group(2))
        text = " ".join(lines[time_idx + 1:]).strip()
        cues.append(Cue(index=idx or len(cues) + 1, start_sec=start, end_sec=end, text=text))
    return cues


def _nearest(cues: List[Cue], t: float, use_start: bool):
    """Return (cue, delta_sec) for the cue boundary closest to time t."""
    best = None
    best_d = None
    for c in cues:
        boundary = c.start_sec if use_start else c.end_sec
        d = boundary - t
        if best_d is None or abs(d) < abs(best_d):
            best_d, best = d, c
    return best, best_d


@dataclass
class BoundaryCheck:
    segment_name: str
    in_delta: Optional[float]
    out_delta: Optional[float]
    open_line: str      # subtitle text the segment opens on
    close_line: str     # subtitle text the segment closes on


def check_segment_boundaries(cues: List[Cue], name, in_sec, out_sec, tol=0.15) -> BoundaryCheck:
    c_in, d_in = _nearest(cues, in_sec, use_start=True)
    c_out, d_out = _nearest(cues, out_sec, use_start=False)
    return BoundaryCheck(
        segment_name=name,
        in_delta=d_in,
        out_delta=d_out,
        open_line=c_in.text if c_in else "",
        close_line=c_out.text if c_out else "",
    )

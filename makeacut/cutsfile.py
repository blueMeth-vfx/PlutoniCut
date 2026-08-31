"""Load and validate cuts.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from .timecode import parse_srt_tc


@dataclass
class Segment:
    name: str
    tc_in: str
    tc_out: str
    note: Optional[str] = None
    # Filled in after parsing, so validation errors point at the raw string.
    in_sec: float = field(default=0.0)
    out_sec: float = field(default=0.0)


@dataclass
class CutsFile:
    timeline_name: str
    clip_hint: str
    srt_offset: str  # "auto" or a number-as-string (seconds)
    segments: List[Segment]


def _require(d: dict, key: str, ctx: str):
    if key not in d:
        raise ValueError(f"cuts.json: campo mancante '{key}' in {ctx}")
    return d[key]


def load_cuts(path: str) -> CutsFile:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    timeline_name = _require(data, "timeline_name", "root")
    clip_hint = _require(data, "clip_hint", "root")
    srt_offset = data.get("srt_offset", "auto")
    raw_segments = _require(data, "segments", "root")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("cuts.json: 'segments' deve essere una lista non vuota")

    segments: List[Segment] = []
    for i, raw in enumerate(raw_segments):
        ctx = f"segments[{i}]"
        name = _require(raw, "name", ctx)
        tc_in = _require(raw, "in", ctx)
        tc_out = _require(raw, "out", ctx)
        seg = Segment(name=name, tc_in=tc_in, tc_out=tc_out, note=raw.get("note"))
        seg.in_sec = parse_srt_tc(tc_in)
        seg.out_sec = parse_srt_tc(tc_out)
        if seg.out_sec <= seg.in_sec:
            raise ValueError(
                f"cuts.json: {ctx} ('{name}') ha out <= in ({tc_out} <= {tc_in})"
            )
        segments.append(seg)

    return CutsFile(
        timeline_name=timeline_name,
        clip_hint=clip_hint,
        srt_offset=str(srt_offset),
        segments=segments,
    )

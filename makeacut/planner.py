"""Pure planning logic: cuts.json segments -> concrete frame plan.

No Resolve objects here. This is the single source of truth shared by the live
(external API) path and the generated Console script, so both produce identical
cuts. Given fps + the SRT reference start, it turns each segment into media-frame
in/out and a cumulative timeline position for its chapter marker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from .cutsfile import Segment
from .timecode import normalize_fps, seconds_to_frames

# AppendToTimeline treats endFrame as EXCLUSIVE: the appended clip covers
# [startFrame, endFrame-1], so its length is endFrame - startFrame. VERIFIED live
# (Resolve Studio 21.0.4.5): passing 240..479 yields a 239-frame item. The SRT
# 'out' is itself an exclusive boundary (end of the last word) -> out_frame maps
# straight to endFrame, giving length (out_sec-in_sec)*fps exactly. Flip only if a
# future build changes this.
END_FRAME_INCLUSIVE = False


@dataclass
class PlanItem:
    name: str
    note: Optional[str]
    start_frame: int          # media-relative, 0-based (for AppendToTimeline)
    end_frame: int            # media-relative, inclusive
    timeline_frame: int       # position of this segment's marker on the timeline
    src_in_sec: float         # for logging
    src_out_sec: float


@dataclass
class Plan:
    fps: float
    srt_start_sec: float
    offset_sec: float
    clip_start_sec: Optional[float]
    items: List[PlanItem]
    warnings: List[str]

    @property
    def total_spoken_sec(self) -> float:
        return sum(i.src_out_sec - i.src_in_sec for i in self.items)


def infer_srt_start_sec(segments: List[Segment]) -> float:
    """Auto: the SRT clock's origin = whole hour below the earliest cue.

    SRT files start their timeline on a round hour (00:00:00 or, for these
    interviews, 01:00:00). Flooring the first segment's in-point to the hour
    recovers that origin without needing the .srt file itself.
    """
    earliest = min(s.in_sec for s in segments)
    return math.floor(earliest / 3600.0) * 3600.0


def build_plan(
    segments: List[Segment],
    fps,
    srt_offset: str,
    clip_start_sec: Optional[float],
    manual_offset_sec: Optional[float] = None,
    clip_frames: Optional[int] = None,
) -> Plan:
    """Compute the frame plan.

    media_time = srt_time - srt_start   (seconds from the clip's first frame)
    A manual offset (CLI --offset or numeric srt_offset) is added on top of that
    to nudge everything, e.g. to compensate a known handle.
    """
    fps = normalize_fps(fps)
    warnings: List[str] = []

    srt_start_sec = infer_srt_start_sec(segments)

    # Resolve the extra shift, if any.
    offset_sec = 0.0
    if manual_offset_sec is not None:
        offset_sec = float(manual_offset_sec)
    elif srt_offset not in ("auto", "", None):
        try:
            offset_sec = float(srt_offset)
        except ValueError:
            warnings.append(
                f"srt_offset '{srt_offset}' non numerico e diverso da 'auto': ignorato"
            )

    # Verification against the clip's real Start TC (log only).
    if clip_start_sec is not None:
        drift = clip_start_sec - srt_start_sec
        if abs(drift) < 0.5:
            warnings.append(
                f"Start TC clip allineato all'inizio SRT ({srt_start_sec/3600:.0f}h): offset di riferimento 0"
            )
        else:
            warnings.append(
                f"Start TC clip ({clip_start_sec:.1f}s) diverso dall'inizio SRT dedotto "
                f"({srt_start_sec:.1f}s): uso l'inizio SRT come origine (media parte da frame 0)"
            )

    items: List[PlanItem] = []
    timeline_cursor = 0
    for seg in segments:
        media_in = seg.in_sec - srt_start_sec + offset_sec
        media_out = seg.out_sec - srt_start_sec + offset_sec
        in_frame = seconds_to_frames(media_in, fps)
        out_frame = seconds_to_frames(media_out, fps)

        if in_frame < 0:
            warnings.append(
                f"'{seg.name}': in negativo ({in_frame}f) rispetto all'inizio media; clampato a 0"
            )
            in_frame = 0
        end_frame = out_frame - 1 if END_FRAME_INCLUSIVE else out_frame
        if end_frame < in_frame:
            end_frame = in_frame  # degenerate 1-frame segment

        if clip_frames and out_frame > clip_frames:
            warnings.append(
                f"'{seg.name}': out a {out_frame}f oltre la fine della clip "
                f"({clip_frames}f): Resolve lo clampa, il segmento sara' troncato"
            )

        length = end_frame - in_frame + (1 if END_FRAME_INCLUSIVE else 0)
        items.append(
            PlanItem(
                name=seg.name,
                note=seg.note,
                start_frame=in_frame,
                end_frame=end_frame,
                timeline_frame=timeline_cursor,
                src_in_sec=seg.in_sec,
                src_out_sec=seg.out_sec,
            )
        )
        timeline_cursor += length

    return Plan(
        fps=fps,
        srt_start_sec=srt_start_sec,
        offset_sec=offset_sec,
        clip_start_sec=clip_start_sec,
        items=items,
        warnings=warnings,
    )

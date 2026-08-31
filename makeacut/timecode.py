"""Timecode <-> frame conversions.

Two timecode dialects live here:

* SRT time      "HH:MM:SS,mmm"  (comma or dot for milliseconds) -> real seconds
* Frame time    "HH:MM:SS:FF"   (colon or ';' for drop-frame)   -> real seconds

Everything internally is expressed as *real seconds* (float) and converted to
frame indices only at the edges, so we never accumulate rounding error across a
40-minute clip.
"""

from __future__ import annotations

import re

# fps values that are really rational (NTSC-family). We normalize the decimal
# the user/Resolve reports to the exact ratio, so round(sec * fps) doesn't drift
# by a frame near a subtitle boundary on long clips.
_RATIONAL_FPS = {
    23.976: 24000 / 1001,
    29.97: 30000 / 1001,
    47.952: 48000 / 1001,
    59.94: 60000 / 1001,
    119.88: 120000 / 1001,
}

_SRT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*$")
_FRAME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})[:;](\d{1,3})\s*$")


def normalize_fps(fps) -> float:
    """Return the true frame rate for a possibly-rounded fps value/string."""
    f = float(str(fps).lower().replace("fps", "").strip())
    for approx, exact in _RATIONAL_FPS.items():
        if abs(f - approx) < 0.01:
            return exact
    return f


def parse_srt_tc(tc: str) -> float:
    """'01:00:37,958' -> seconds (float). Accepts ',' or '.' before ms."""
    m = _SRT_RE.match(tc)
    if not m:
        raise ValueError(f"Timecode SRT non valido: {tc!r} (atteso HH:MM:SS,mmm)")
    h, mnt, s, ms = m.groups()
    ms = ms.ljust(3, "0")  # '95' -> '950'
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms) / 1000.0


def parse_frame_tc(tc: str, fps) -> float:
    """'01:00:00:00' -> seconds (float), using fps to weigh the frame field.

    Drop-frame ';' is parsed like non-drop here: the sub-frame error is <2 frames
    over an hour and we only use this value for offset/verification logging.
    """
    m = _FRAME_RE.match(tc)
    if not m:
        raise ValueError(f"Timecode a frame non valido: {tc!r} (atteso HH:MM:SS:FF)")
    h, mnt, s, ff = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ff) / normalize_fps(fps)


def seconds_to_frames(seconds: float, fps) -> int:
    """Real seconds -> nearest whole frame index."""
    return int(round(seconds * normalize_fps(fps)))


def frames_to_tc(frames: int, fps) -> str:
    """Frame index -> 'HH:MM:SS:FF' (non-drop), for readable logs."""
    f = normalize_fps(fps)
    fps_int = int(round(f))
    total = int(frames)
    ff = total % fps_int
    total //= fps_int
    s = total % 60
    total //= 60
    mnt = total % 60
    h = total // 60
    return f"{h:02d}:{mnt:02d}:{s:02d}:{ff:02d}"


def seconds_to_clock(seconds: float) -> str:
    """Seconds -> 'MM:SS' for durations in the summary log."""
    seconds = int(round(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"

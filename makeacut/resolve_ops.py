"""Resolve-object operations: find clip, read properties, build timeline, markers.

Every function takes live API objects. Nothing here computes cut points - that's
planner.py. This module just executes a Plan against the current project.
"""

from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

from .planner import Plan


# ---------------------------------------------------------------- media pool

def _walk_folders(folder):
    """Yield every folder in the tree, depth-first."""
    yield folder
    for sub in folder.GetSubFolderList() or []:
        yield from _walk_folders(sub)


def _all_clips(media_pool):
    clips = []
    root = media_pool.GetRootFolder()
    if not root:
        return clips
    for folder in _walk_folders(root):
        for clip in folder.GetClipList() or []:
            clips.append(clip)
    return clips


def find_clip(project, media_pool, clip_hint: str) -> Tuple[Optional[object], List[str]]:
    """Locate the interview clip. Returns (clip, log_lines).

    Order: current media-pool selection -> fuzzy match on clip_hint across all
    folders (recursive). Video clips only; non-media (timelines, gens) skipped.
    """
    log: List[str] = []

    # 1) current selection, if the API build exposes it
    try:
        selected = media_pool.GetSelectedClips()
        if selected:
            sel = list(selected.values()) if isinstance(selected, dict) else list(selected)
            sel = [c for c in sel if _is_media(c)]
            if len(sel) == 1:
                log.append(f"Uso la clip selezionata nel media pool: {sel[0].GetName()}")
                return sel[0], log
            if len(sel) > 1:
                log.append(f"{len(sel)} clip selezionate: ignoro la selezione, passo al fuzzy match")
    except Exception:
        pass  # older builds: no GetSelectedClips

    # 2) fuzzy match on the hint
    candidates = [c for c in _all_clips(media_pool) if _is_media(c)]
    if not candidates:
        log.append("Nessuna clip media trovata nel media pool")
        return None, log

    hint = clip_hint.lower()
    scored = []
    for c in candidates:
        name = c.GetName() or ""
        low = name.lower()
        contains = hint in low
        ratio = difflib.SequenceMatcher(None, hint, low).ratio()
        score = (1.0 if contains else 0.0) + ratio
        scored.append((score, name, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best_name, best_clip = scored[0]
    log.append(f"Fuzzy match su '{clip_hint}': '{best_name}' (score {best_score:.2f})")
    runners = [f"{n} ({s:.2f})" for s, n, _ in scored[1:4]]
    if runners:
        log.append("Altri candidati: " + ", ".join(runners))
    if best_score < 0.5:
        log.append("ATTENZIONE: match debole, verifica che sia la clip giusta")
    return best_clip, log


def _is_media(clip) -> bool:
    try:
        return (clip.GetClipProperty("Type") or "").lower() not in ("timeline",)
    except Exception:
        return True


def read_clip_props(clip) -> dict:
    """Pull fps and Start TC (and a couple extras for logging)."""
    fps = clip.GetClipProperty("FPS")
    start_tc = clip.GetClipProperty("Start TC")
    return {
        "name": clip.GetName(),
        "fps": fps,
        "start_tc": start_tc,
        "duration": clip.GetClipProperty("Duration"),
        "path": clip.GetClipProperty("File Path"),
    }


# ---------------------------------------------------------------- timeline

def create_unique_timeline(media_pool, name: str) -> Tuple[object, str]:
    """Create an empty timeline; if the name exists, suffix ' (n)'."""
    final = name
    tl = media_pool.CreateEmptyTimeline(final)
    n = 2
    while tl is None and n < 100:
        final = f"{name} ({n})"
        tl = media_pool.CreateEmptyTimeline(final)
        n += 1
    if tl is None:
        raise RuntimeError(f"CreateEmptyTimeline ha restituito None per '{name}'")
    return tl, final


def append_plan(media_pool, clip, plan: Plan) -> List[Tuple[str, bool, str]]:
    """Append each plan item as a clip on the timeline.

    Returns per-segment (name, ok, detail). Items are appended one at a time so a
    single bad segment reports itself instead of failing the whole batch.
    """
    results: List[Tuple[str, bool, str]] = []
    for item in plan.items:
        clip_info = {
            "mediaPoolItem": clip,
            "startFrame": int(item.start_frame),
            "endFrame": int(item.end_frame),
        }
        try:
            appended = media_pool.AppendToTimeline([clip_info])
            ok = bool(appended)
            detail = "" if ok else "AppendToTimeline ha restituito lista vuota"
        except Exception as exc:
            ok, detail = False, f"eccezione: {exc}"
        results.append((item.name, ok, detail))
    return results


def add_chapter_markers(timeline, plan: Plan, color: str = "Yellow") -> int:
    """Add one colored marker at the start of each segment. Returns count added."""
    added = 0
    for item in plan.items:
        note = item.note or ""
        try:
            ok = timeline.AddMarker(
                int(item.timeline_frame), color, item.name, note, 1, ""
            )
            if ok:
                added += 1
        except Exception:
            pass
    return added

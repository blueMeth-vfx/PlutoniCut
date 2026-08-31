"""Generate a self-contained script for Resolve's internal Console (Py3).

Fallback for when external scripting is unavailable (Resolve free on some
platforms). The generated file embeds the already-computed Plan as plain data and
re-does clip lookup + timeline build + markers using only the `resolve` global
that the Console provides. Same logic, different execution surface.
"""

from __future__ import annotations

import json
from typing import Optional

from .planner import Plan

_TEMPLATE = '''# --- MakeACut :: script generato per la Console interna di Resolve ---
# Incolla in: Workspace > Console > (Py3), poi Invio.
# Non richiede scripting esterno.

TIMELINE_NAME = {timeline_name}
CLIP_HINT     = {clip_hint}
MARKER_COLOR  = {marker_color}
ITEMS         = {items}

import difflib

resolve = resolve if "resolve" in dir() else bmd.scriptapp("Resolve")
pm = resolve.GetProjectManager()
project = pm.GetCurrentProject()
media_pool = project.GetMediaPool()

def walk(folder):
    yield folder
    for sub in (folder.GetSubFolderList() or []):
        for f in walk(sub):
            yield f

def all_clips(mp):
    out = []
    root = mp.GetRootFolder()
    for folder in walk(root):
        for c in (folder.GetClipList() or []):
            out.append(c)
    return out

def find_clip(mp, hint):
    cands = all_clips(mp)
    scored = []
    for c in cands:
        name = c.GetName() or ""
        low = name.lower()
        contains = hint.lower() in low
        ratio = difflib.SequenceMatcher(None, hint.lower(), low).ratio()
        scored.append(((1.0 if contains else 0.0) + ratio, name, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][2] if scored else None

clip = find_clip(media_pool, CLIP_HINT)
if not clip:
    print("MakeACut: nessuna clip trovata per hint", CLIP_HINT)
else:
    print("MakeACut: clip =", clip.GetName())
    tl = media_pool.CreateEmptyTimeline(TIMELINE_NAME)
    n = 2
    while tl is None and n < 100:
        tl = media_pool.CreateEmptyTimeline(TIMELINE_NAME + " (%d)" % n)
        n += 1
    ok_count = 0
    for it in ITEMS:
        info = {{"mediaPoolItem": clip,
                "startFrame": int(it["start_frame"]),
                "endFrame": int(it["end_frame"])}}
        res = media_pool.AppendToTimeline([info])
        if res:
            ok_count += 1
        else:
            print("MakeACut: FALLITO segmento", it["name"])
    for it in ITEMS:
        tl.AddMarker(int(it["timeline_frame"]), MARKER_COLOR,
                     it["name"], it.get("note") or "", 1, "")
    print("MakeACut: %d/%d segmenti, timeline '%s'" % (ok_count, len(ITEMS), tl.GetName()))
'''


def generate_console_script(
    plan: Plan,
    timeline_name: str,
    clip_hint: str,
    marker_color: str = "Yellow",
) -> str:
    items = [
        {
            "name": it.name,
            "note": it.note,
            "start_frame": it.start_frame,
            "end_frame": it.end_frame,
            "timeline_frame": it.timeline_frame,
        }
        for it in plan.items
    ]
    return _TEMPLATE.format(
        timeline_name=json.dumps(timeline_name, ensure_ascii=False),
        clip_hint=json.dumps(clip_hint, ensure_ascii=False),
        marker_color=json.dumps(marker_color, ensure_ascii=False),
        items=json.dumps(items, ensure_ascii=False, indent=2),
    )

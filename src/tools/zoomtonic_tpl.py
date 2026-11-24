"""
ZoomTonic - plugin per DaVinci Resolve (Workspace > Scripts > Utility > ZoomTonic)
Fa parte di PlutoniCut, la casa degli strumenti Plutonic.

Zoom dinamici automatici sulle clip di una traccia video: sui tagli, all'inizio delle
frasi e nei momenti di enfasi della voce, col ritmo, l'intensita', il punto d'ancoraggio
e lo stile (scatto, morbido, rapido) che scegli tu. Gli zoom vanno in una composizione
Fusion "ZoomTonic" su ogni clip: cambia solo il video, l'audio non si tocca, e si
tolgono con un clic.

Self-contained: gira con il Python interno di Resolve; serve ffmpeg per ascoltare la voce.
"""

import json
import math
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                      # Python < 3.7
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

try:
    resolve  # iniettata quando si lancia da Workspace > Scripts
except NameError:
    import DaVinciResolveScript as _dvr
    resolve = _dvr.scriptapp("Resolve")

APP = "ZoomTonic"

#@@CORE@@

#@@MEDIA@@

# --------------------------------------------------------------------------
# ZoomTonic
# --------------------------------------------------------------------------
COMP = "ZoomTonic"            # nome della composizione Fusion e del suo Transform
PACES = {   # probabilita' di zoom su un taglio, pausa minima tra zoom (s), durata zoom (s)
    "molto_calmo": (0.25, 8.0, (3.0, 6.0)),
    "calmo": (0.45, 5.0, (2.5, 5.0)),
    "moderato": (0.65, 3.5, (2.0, 4.0)),
    "energico": (0.85, 2.0, (1.2, 3.0)),
    "iperattivo": (1.00, 1.0, (0.8, 2.0)),
}
STYLES = ("smooth", "jump", "snap")
SET = {"look": load_theme(), "ontop": True, "track": 1, "range": False, "from": "", "to": "",
       "voice": 0, "pace": "moderato", "t_speech": True, "t_emph": True,
       "zmin": 110, "zmax": 125, "styles": ["smooth", "jump"], "ax": 0.5, "ay": 0.4,
       "drift": False, "seed": 1}
TITLES = ("ZoomTonic",)
SRC = {"name": "", "key": "", "fps": 24.0, "tracks": {"video": [], "audio": []}, "ok": False, "zoomed": 0}
AN = {"ver": 0, "sig": "", "a": 0, "b": 0, "fps": 24.0, "clips": [], "voice": [], "frame": b"", "user": 0}
PLAN = {"ver": 0, "zooms": []}


def _apply_settings(d, save=True):
    _apply_look(d)
    for k in ("track", "voice", "seed"):
        if k in d and str(d[k]).isdigit():
            SET[k] = int(d[k])
    for k in ("range", "t_speech", "t_emph", "drift"):
        if k in d:
            SET[k] = bool(d[k])
    for k in ("from", "to"):
        if isinstance(d.get(k), str):
            SET[k] = d[k].strip()[:20]
    for k, lo, hi in (("zmin", 101, 300), ("zmax", 101, 300), ("ax", 0, 1), ("ay", 0, 1)):
        try:
            if k in d:
                SET[k] = max(lo, min(hi, float(d[k])))
        except (TypeError, ValueError):
            pass
    if SET["zmax"] < SET["zmin"]:
        SET["zmax"] = SET["zmin"]
    if d.get("pace") in PACES:
        SET["pace"] = d["pace"]
    if isinstance(d.get("styles"), list):
        st = [x for x in d["styles"] if x in STYLES]
        SET["styles"] = st or ["smooth"]
    if save:
        _save_settings()


def _zoomed(tl, ti):
    n = 0
    for it in tl.GetItemListInTrack("video", ti) or []:
        try:
            if COMP in (it.GetFusionCompNameList() or []):
                n += 1
        except Exception:
            pass
    return n


def on_refresh(ev=None):
    tl = current_timeline()
    if not tl:
        SRC.update(name="", key="", ok=False, tracks={"video": [], "audio": []}, zoomed=0)
        set_status("Apri in Resolve la timeline da animare.")
        return None
    tr = track_info(tl)
    if not 1 <= SET["track"] <= len(tr["video"]):
        SET["track"] = next((t["i"] for t in tr["video"] if t["n"]), 1)
    if SET["voice"] > len(tr["audio"]) or (not SET["voice"] and tr["audio"]):
        SET["voice"] = next((t["i"] for t in tr["audio"] if t["n"]), 0)
    SRC.update(name=tl.GetName(), key=tl_key(tl), fps=tl_fps(tl), tracks=tr, ok=True,
               zoomed=_zoomed(tl, SET["track"]))
    set_status("Timeline «%s»: %d tracce video." % (SRC["name"], len(tr["video"])))
    return tl


def _idle():
    tl = current_timeline()
    if (tl_key(tl) if tl else "") != SRC["key"]:
        on_refresh()


def _sig():
    return json.dumps([SRC["key"], SET["track"], SET["voice"], SET["range"], SET["from"], SET["to"]])


def _grab_frame(it, fps):
    """Un fotogramma (JPEG) dal centro della clip, per scegliere il punto d'ancoraggio."""
    mpi = it.GetMediaPoolItem()
    path = mpi.GetClipProperty("File Path") if mpi else ""
    if not (FFMPEG and path and os.path.exists(path)):
        return b""
    k = clip_fps(mpi, fps)
    t = ((it.GetSourceStartFrame() or 0) + it.GetDuration() * k / fps / 2) / k
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", "%.3f" % t, "-i", path, "-frames:v", "1",
                            "-vf", "scale=640:-2", "-f", "image2", "-vcodec", "mjpeg", "-q:v", "4", "-"],
                           capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NOWIN, timeout=60)
        return r.stdout
    except Exception:
        return b""


def on_analyze(ev=None):
    """Legge le clip della traccia scelta e ascolta la voce."""
    tl = on_refresh()
    if not tl:
        set_log("Apri in Resolve la timeline da animare."); return
    a, b, err = section(tl, SET)
    if err:
        set_log(err); return
    fps = tl_fps(tl)
    items = [it for it in (tl.GetItemListInTrack("video", SET["track"]) or []) if it.GetEnd() > a and it.GetStart() < b]
    if not items:
        set_log("La traccia V%d non ha clip nel tratto scelto." % SET["track"]); return
    clips, user = [], 0
    for it in items:
        try:
            names = it.GetFusionCompNameList() or []
        except Exception:
            names = []
        other = bool(names) and COMP not in names            # ha gia' una sua composizione Fusion
        user += other
        clips.append((it.GetStart(), it.GetEnd(), other))
    voice = []
    if SET["voice"] and (SET["t_speech"] or SET["t_emph"]) and FFMPEG:
        voice = track_envelope(tl, SET["voice"], a, b, lambda m: set_status("Ascolto la voce: " + m))
    mid = items[len(items) // 2]
    AN.update(ver=AN["ver"] + 1, sig=_sig(), a=a, b=b, fps=fps, clips=clips, voice=voice,
              frame=_grab_frame(mid, fps), user=user)
    make_plan()
    set_log("Lette %d clip della traccia V%d%s. Clicca sull'immagine il punto da tenere al centro dello zoom "
            "(es. il volto), poi «Applica gli zoom».%s" % (
                len(clips), SET["track"], (" e ascoltata la voce (A%d)" % SET["voice"]) if voice else "",
                ("\n%d clip hanno già una loro composizione Fusion: le salto per non toccarla." % user) if user else ""))
    set_status("Pronto: guarda l'anteprima, poi «Applica gli zoom».")


def make_plan():
    """Gli zoom: [(da, a, zoom, stile)] in fotogrammi di timeline, dentro una sola clip."""
    if not AN["clips"]:
        PLAN.update(ver=PLAN["ver"] + 1, zooms=[])
        return
    a, b, fps = AN["a"], AN["b"], AN["fps"]
    p_cut, gap, (h0, h1) = PACES[SET["pace"]]
    rnd = random.Random(SET["seed"])
    F = lambda i: a + int(round(i / ENV_RATE * fps))
    ev = [(s, "taglio") for s, e, o in AN["clips"] if s >= a]
    if AN["voice"]:
        if SET["t_speech"]:
            thr = auto_threshold(AN["voice"])
            ev += [(F(i0), "frase") for i0, _ in speech_ranges(AN["voice"], thr, 0.45, 0.3)[1:]]
        if SET["t_emph"]:
            ev += [(F(i), "enfasi") for i in emphasis_points(AN["voice"], 2.0)]
    ev.sort()
    zooms, free = [], a
    for f, kind in ev:
        if f < free:
            continue
        clip = next(((s, e) for s, e, o in AN["clips"] if s <= f < e and not o), None)
        if not clip:
            continue
        p = p_cut if kind == "taglio" else (0.6 + p_cut) / 2
        if rnd.random() > p:
            continue
        hold = int(rnd.uniform(h0, h1) * fps)
        end = min(f + hold, clip[1], b)
        if end - f < int(0.5 * fps):
            continue
        z = rnd.uniform(SET["zmin"], SET["zmax"]) / 100.0
        zooms.append((f, end, round(z, 3), rnd.choice(SET["styles"]), kind))
        free = end + int(gap * fps)
    PLAN.update(ver=PLAN["ver"] + 1, zooms=zooms)


def _keys(z0, z1, z, style, fps, dur):
    """Fotogrammi chiave della dimensione (relativi alla clip) per uno zoom [z0, z1)."""
    if style == "jump":                          # scatto: dentro e fuori di colpo
        k = [(z0 - 1, 1.0), (z0, z), (z1 - 1, z), (z1, 1.0)]
    elif style == "snap":                        # rapido: entra veloce, esce di colpo
        r = max(2, min(int(0.25 * fps), (z1 - z0) // 3))
        k = [(z0, 1.0), (z0 + r, z), (z1 - 1, z), (z1, 1.0)]
    else:                                        # morbido: entra ed esce dolce
        r = max(3, min(int(0.7 * fps), (z1 - z0) // 3))
        k = [(z0, 1.0), (z0 + r, z), (z1 - r, z), (z1, 1.0)]
    return [(f, v) for f, v in k if 0 <= f < dur]


def _zoom_tool(it):
    """La composizione ZoomTonic della clip (creata se non c'e') e il suo Transform, nuovo."""
    names = it.GetFusionCompNameList() or []
    if names and COMP not in names:
        return None, None                        # composizione dell'utente: non si tocca
    if COMP in names:
        comp = it.LoadFusionCompByName(COMP) or it.GetFusionCompByName(COMP)
    else:
        comp = it.AddFusionComp()
        if comp:
            it.RenameFusionCompByName(it.GetFusionCompNameList()[-1], COMP)
    if not comp:
        return None, None
    mi, mo = comp.FindTool("MediaIn1"), comp.FindTool("MediaOut1")
    old = comp.FindTool(COMP)
    comp.Lock()
    if old:
        old.Delete()
    tf = comp.AddTool("Transform", -32768, -32768)
    tf.SetAttrs({"TOOLS_Name": COMP})
    tf.ConnectInput("Input", mi)
    mo.ConnectInput("Input", tf)
    comp.Unlock()
    return comp, tf


def on_apply(ev=None):
    """Mette gli zoom nelle clip: composizione Fusion «ZoomTonic», solo video."""
    tl = on_refresh()
    if not tl:
        set_log("Apri in Resolve la timeline da animare."); return
    if AN["sig"] != _sig() or not AN["clips"]:
        on_analyze()
    make_plan()
    zooms = PLAN["zooms"]
    if not zooms:
        set_log("Nessuno zoom con queste regole: prova un ritmo più energico."); return
    fps = tl_fps(tl)
    items = tl.GetItemListInTrack("video", SET["track"]) or []
    piv = {1: SET["ax"], 2: 1.0 - SET["ay"]}       # Fusion: y dal basso
    rnd = random.Random(SET["seed"] + 7)
    done, clips, skipped = 0, 0, 0
    for n, it in enumerate(items):
        s, e = it.GetStart(), it.GetEnd()
        mine = [z for z in zooms if s <= z[0] < e]
        if not mine:
            continue
        if n % 5 == 0:
            set_status("Metto gli zoom: clip %d di %d…" % (n + 1, len(items)))
        comp, tf = _zoom_tool(it)
        if not tf:
            skipped += 1
            continue
        dur = e - s
        comp.Lock()
        tf.SetInput("Pivot", piv)
        tf.SetInput("Center", piv)
        tf.AddModifier("Size", "BezierSpline")
        keys = {0: 1.0}
        for z0, z1, z, st, _ in mine:
            for f, v in _keys(z0 - s, z1 - s, z, st, fps, dur):
                keys[f] = v
        for f in sorted(keys):
            tf.SetInput("Size", keys[f], f)
        if SET["drift"]:                         # leggero movimento di camera durante gli zoom
            try:
                tf.AddModifier("Center", "XYPath")
                path = tf.Center.GetConnectedOutput().GetTool()
                for z0, z1, z, st, _ in mine:
                    path.SetInput("X", piv[1], max(0, z0 - s))
                    path.SetInput("Y", piv[2], max(0, z0 - s))
                    for f in range(z0 - s + 6, z1 - s - 3, max(6, int(fps / 2))):
                        size = tf.GetInput("Size", f) or 1.0
                        amp = 0.35 * (size - 1.0) / (2 * size)   # mai oltre il bordo dell'immagine
                        path.SetInput("X", piv[1] + rnd.uniform(-amp, amp), f)
                        path.SetInput("Y", piv[2] + rnd.uniform(-amp, amp), f)
                    path.SetInput("X", piv[1], z1 - s)
                    path.SetInput("Y", piv[2], z1 - s)
            except Exception:
                pass
        comp.Unlock()
        done += len(mine)
        clips += 1
    set_log("Fatto: %d zoom su %d clip della traccia V%d di «%s».%s\n\nSolo il video è cambiato: l'audio è "
            "intatto. Gli zoom sono nella composizione Fusion «%s» di ogni clip (modificabili nella pagina "
            "Fusion); «Togli gli zoom» li rimuove tutti." % (
                done, clips, SET["track"], tl.GetName(),
                (" %d clip saltate (hanno una loro composizione Fusion)." % skipped) if skipped else "", COMP))
    set_status("Zoom applicati: %d." % done)
    on_refresh()


def on_undo(ev=None):
    """Toglie gli zoom ZoomTonic dalla traccia scelta (la clip torna com'era)."""
    tl = current_timeline()
    if not tl:
        return
    n = 0
    for it in tl.GetItemListInTrack("video", SET["track"]) or []:
        try:
            if COMP not in (it.GetFusionCompNameList() or []):
                continue
            comp = it.GetFusionCompByName(COMP)
            tf = comp.FindTool(COMP) if comp else None
            if tf:
                comp.Lock()
                comp.FindTool("MediaOut1").ConnectInput("Input", comp.FindTool("MediaIn1"))
                tf.Delete()
                comp.Unlock()
                n += 1
        except Exception:
            pass
    set_log("Tolti gli zoom da %d clip della traccia V%d." % (n, SET["track"]) if n
            else "Nessuno zoom ZoomTonic sulla traccia V%d." % SET["track"])
    on_refresh()


def on_shuffle(ev=None):
    SET["seed"] = random.randrange(1, 10 ** 6)
    _save_settings()
    make_plan()


def _plan_json():
    if AN["sig"] == _sig():
        make_plan()
    return {"ver": PLAN["ver"], "a": AN["a"], "b": AN["b"], "fps": AN["fps"], "zooms": PLAN["zooms"],
            "clips": AN["clips"], "voice": [int(round(max(v, -99))) for v in AN["voice"][::5]], "frame": bool(AN["frame"])}


def _state():
    return {"set": SET, "log": STATE["log"], "status": STATE["status"], "busy": STATE["busy"],
            "quit": STATE["quit"], "src": SRC, "playhead": PLAYHEAD, "anver": AN["ver"],
            "fresh": AN["sig"] == _sig(), "ffmpeg": bool(FFMPEG), "paces": list(PACES)}


GETS = {"/api/plan": _plan_json, "/api/frame": lambda: (AN["frame"] or b"", "image/jpeg")}
CMDS = {"refresh": on_refresh, "analyze": on_analyze, "apply": on_apply, "undo": on_undo,
        "shuffle": on_shuffle, "playhead": on_playhead, "goto": on_goto, "quit": on_close}
QUIET = ("playhead", "goto", "shuffle")

PAGE_HTML = r"""@@PAGE@@"""


def setup():
    """Prepara ZoomTonic: da solo o dentro PlutoniCut (la casa degli strumenti)."""
    _load_settings()
    set_log("1) Scegli la traccia video e la voce   2) Ritmo, quando zoomare e quanto   3) Analizza e "
            "clicca il punto d'ancoraggio   4) Applica gli zoom (si tolgono con «Togli gli zoom»).")
    try:
        on_refresh()
    except Exception:
        pass


def main():
    setup()
    run_app((1180, 900), idle=_idle, quiet=QUIET)


main()

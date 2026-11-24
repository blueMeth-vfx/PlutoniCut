"""
PlutoniCamera - plugin per DaVinci Resolve (Workspace > Scripts > Utility > PlutoniCamera)
Fa parte di PlutoniCut, la casa degli strumenti Plutonic.

Cambi camera automatici per riprese multicamera in sync (es. un'intervista): sceglie
quando cambiare (pause, chi parla, enfasi, tagli esistenti) e quale camera (priorita',
mai la stessa due volte di fila, durata minima e massima dell'inquadratura) e mette il
risultato su una traccia video NUOVA in cima. L'audio non si tocca.

Self-contained: gira con il Python interno di Resolve; serve ffmpeg per ascoltare l'audio.
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

APP = "PlutoniCamera"

#@@CORE@@

#@@MEDIA@@

# --------------------------------------------------------------------------
# PlutoniCamera
# --------------------------------------------------------------------------
TRACK_NAME = "PlutoniCamera"
SET = {"look": load_theme(), "ontop": True, "range": False, "from": "", "to": "",
       "cams": [], "prio": {}, "mic": {}, "voice": [],
       "t_cuts": True, "t_pause": True, "t_speaker": True, "t_emph": False,
       "pause_min": 0.6, "min_shot": 2.5, "max_shot": 12.0, "first": 0,
       "disable_src": True, "copy": False, "seed": 1}
TITLES = ("PlutoniCamera",)
SRC = {"name": "", "key": "", "fps": 24.0, "tracks": {"video": [], "audio": []}, "ok": False, "done": False}
AN = {"ver": 0, "sig": "", "a": 0, "b": 0, "fps": 24.0, "voice": [], "mics": {}, "cover": {}, "cuts": []}
PLAN = {"ver": 0, "segs": [], "trig": []}


def _apply_settings(d, save=True):
    _apply_look(d)
    for k in ("cams", "voice"):
        if isinstance(d.get(k), list):
            SET[k] = [int(x) for x in d[k] if str(x).isdigit()]
    for k in ("prio", "mic"):
        if isinstance(d.get(k), dict):
            SET[k] = {str(a): int(v) for a, v in d[k].items() if str(v).lstrip("-").isdigit()}
    for k in ("range", "t_cuts", "t_pause", "t_speaker", "t_emph", "disable_src", "copy"):
        if k in d:
            SET[k] = bool(d[k])
    for k in ("from", "to"):
        if isinstance(d.get(k), str):
            SET[k] = d[k].strip()[:20]
    for k, lo, hi in (("pause_min", 0.2, 5), ("min_shot", 0.5, 30), ("max_shot", 1, 120)):
        try:
            if k in d:
                SET[k] = max(lo, min(hi, float(d[k])))
        except (TypeError, ValueError):
            pass
    for k in ("first", "seed"):
        if k in d and str(d[k]).isdigit():
            SET[k] = int(d[k])
    if save:
        _save_settings()


def on_refresh(ev=None):
    tl = current_timeline()
    if not tl:
        SRC.update(name="", key="", ok=False, tracks={"video": [], "audio": []}, done=False)
        set_status("Apri in Resolve la timeline con le camere in sync.")
        return None
    tr = track_info(tl)
    SRC.update(name=tl.GetName(), key=tl_key(tl), fps=tl_fps(tl), tracks=tr, ok=True,
               done=any(t["name"] == TRACK_NAME for t in tr["video"]))
    vids = [t["i"] for t in tr["video"] if t["n"] and t["name"] != TRACK_NAME]
    if len([c for c in SET["cams"] if c in vids]) < 2:
        SET["cams"] = vids
    if not SET["voice"] or any(v > len(tr["audio"]) for v in SET["voice"]):
        SET["voice"] = [t["i"] for t in tr["audio"] if t["n"]][:1]
    set_status("Timeline «%s»: %d tracce video, %d audio." % (SRC["name"], len(tr["video"]), len(tr["audio"])))
    return tl


def _idle():
    tl = current_timeline()
    if (tl_key(tl) if tl else "") != SRC["key"]:
        on_refresh()


def _sig():
    return json.dumps([SRC["key"], SET["range"], SET["from"], SET["to"], sorted(SET["cams"]), sorted(SET["voice"]),
                       sorted((k, v) for k, v in SET["mic"].items() if v)])


def on_analyze(ev=None):
    """Ascolta voce e microfoni e legge dove ogni camera ha immagini."""
    tl = on_refresh()
    if not tl:
        set_log("Apri in Resolve la timeline con le camere in sync."); return
    if len(SET["cams"]) < 2:
        set_log("Scegli almeno due tracce camera."); return
    a, b, err = section(tl, SET)
    if err:
        set_log(err); return
    fps = tl_fps(tl)
    cover, cuts = {}, set()
    for c in SET["cams"]:
        rr = []
        for it in tl.GetItemListInTrack("video", c) or []:
            s, e = it.GetStart(), it.GetEnd()
            if e > a and s < b:
                rr.append((max(s, a), min(e, b)))
                for x in (s, e):
                    if a < x < b:
                        cuts.add(x)
        cover[c] = rr
    need_audio = SET["t_pause"] or SET["t_emph"] or (SET["t_speaker"] and any(SET["mic"].values()))
    voice, mics = [], {}
    if need_audio and not FFMPEG:
        set_log("ffmpeg non trovato: senza, i cambi si fanno solo sui tagli esistenti e a durata massima.")
    elif need_audio:
        prog = lambda m: set_status("Ascolto l'audio: " + m)
        cache = {}
        for ti in set(SET["voice"]) | {v for v in SET["mic"].values() if v}:
            cache[ti] = track_envelope(tl, ti, a, b, prog)
        voice = merge_envs([cache[t] for t in SET["voice"] if t in cache])
        mics = {c: cache[SET["mic"][str(c)]] for c in SET["cams"] if SET["mic"].get(str(c)) in cache}
    AN.update(ver=AN["ver"] + 1, sig=_sig(), a=a, b=b, fps=fps, voice=voice, mics=mics, cover=cover, cuts=sorted(cuts))
    make_plan()
    set_log("Analisi fatta su «%s» (%s): l'anteprima mostra i cambi camera. Cambia le regole: "
            "l'anteprima si aggiorna da sola." % (tl.GetName(), fmt_dur((b - a) / fps)))
    set_status("Pronto: guarda l'anteprima, poi «Monta i cambi».")


def fmt_dur(s):
    s = int(round(s))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


def _covered(c, f):
    return any(s <= f < e for s, e in AN["cover"].get(c, []))


def _speakers():
    """Chi parla, finestra per finestra (solo camere con un microfono): indice camera o 0."""
    mics = AN["mics"]
    if not mics:
        return []
    thr = {c: auto_threshold(env) for c, env in mics.items()}
    n = min(len(e) for e in mics.values())
    out, cur, hold = [], 0, 0
    for i in range(n):
        best, lv = 0, -999.0
        for c, env in mics.items():
            if env[i] > thr[c] and env[i] - thr[c] > lv:
                best, lv = c, env[i] - thr[c]
        if best and best != cur:                 # un cambio vale solo se dura 0,4 s
            hold += 1
            if hold >= 40 or not cur:
                cur, hold = best, 0
        else:
            hold = 0
        out.append(cur)
    return out


def make_plan():
    """Le inquadrature: [(da, a, camera)] in fotogrammi di timeline."""
    if not AN["cover"]:
        PLAN.update(ver=PLAN["ver"] + 1, segs=[], trig=[])
        return
    a, b, fps = AN["a"], AN["b"], AN["fps"]
    cams = [c for c in SET["cams"] if c in AN["cover"]]
    rnd = random.Random(SET["seed"])
    w = {c: max(1, SET["prio"].get(str(c), 3)) for c in cams}
    F = lambda i: a + int(round(i / ENV_RATE * fps))
    trig = []                                         # (fotogramma, tipo, camera preferita)
    if SET["t_cuts"]:
        trig += [(f, "taglio", 0) for f in AN["cuts"]]
    if AN["voice"] and (SET["t_pause"] or SET["t_emph"]):
        thr = auto_threshold(AN["voice"])
        if SET["t_pause"]:
            sp = speech_ranges(AN["voice"], thr, SET["pause_min"], 0.15)
            trig += [(max(a, F(i0) - int(0.12 * fps)), "pausa", 0) for i0, _ in sp[1:]]
        if SET["t_emph"]:
            trig += [(F(i), "enfasi", 0) for i in emphasis_points(AN["voice"], 1.5)]
    spk = _speakers() if SET["t_speaker"] else []
    if spk:
        for i in range(1, len(spk)):
            if spk[i] and spk[i] != spk[i - 1]:
                trig.append((F(i), "parla", spk[i]))
    trig.sort()

    def pick(f, cur, pref=0):
        av = [c for c in cams if c != cur and _covered(c, f)]
        if pref and pref in av:
            return pref
        if not av:
            return 0
        tot = sum(w[c] for c in av)
        x = rnd.random() * tot
        for c in av:
            x -= w[c]
            if x <= 0:
                return c
        return av[-1]

    mn, mx = int(SET["min_shot"] * fps), int(SET["max_shot"] * fps)
    first = SET["first"] if SET["first"] in cams else max(cams, key=lambda c: (w[c], -c))
    if not _covered(first, a):
        first = pick(a, 0) or first
    segs, cur, last = [], first, a
    for f, kind, pref in trig + [(b, "fine", 0)]:
        while f - last > mx and last + mx < b - mn:   # inquadratura troppo lunga: si cambia
            nxt = pick(last + mx, cur)
            if not nxt:
                break
            segs.append((last, last + mx, cur))
            cur, last = nxt, last + mx
        if kind == "fine":
            break
        if f - last < mn or b - f < mn:              # niente inquadrature piu' corte del minimo
            continue
        if kind == "parla" and pref == cur:
            continue
        nxt = pick(f, cur, pref)
        if nxt:
            segs.append((last, f, cur))
            cur, last = nxt, f
    segs.append((last, b, cur))
    out = []                                          # dove la camera non ha immagini: un'altra
    by_prio = sorted(cams, key=lambda x: -w[x])
    for s0, e, c in segs:
        f = s0
        while f < e:
            cc = c if _covered(c, f) else next((x for x in by_prio if _covered(x, f)), 0)
            ends = [e]
            if cc:                                    # fin dove questa camera ha immagini
                ends += [y for x, y in AN["cover"][cc] if x <= f < y]
            if cc != c:                               # appena la camera scelta torna, si torna a lei
                ends += [x for x, y in AN["cover"][c] if f < x < e]
            if not cc:                                # nessuna camera: fino alla prima che riparte
                ends += [x for k in cams for x, y in AN["cover"][k] if f < x < e]
            g = min(ends)
            if out and out[-1][2] == cc and out[-1][1] == f:
                out[-1] = (out[-1][0], g, cc)
            else:
                out.append((f, g, cc))
            f = g
    PLAN.update(ver=PLAN["ver"] + 1, segs=out, trig=[(f, k) for f, k, _ in trig])


def on_shuffle(ev=None):
    SET["seed"] = random.randrange(1, 10 ** 6)
    _save_settings()
    make_plan()


def on_apply(ev=None):
    """I cambi camera su una traccia video nuova in cima (solo video, l'audio non si tocca)."""
    tl = on_refresh()
    if not tl:
        set_log("Apri in Resolve la timeline con le camere in sync."); return
    if AN["sig"] != _sig() or not AN["cover"]:
        on_analyze()
    make_plan()
    segs = [s for s in PLAN["segs"] if s[2]]
    if not segs:
        set_log("Nessuna inquadratura da montare: controlla le tracce camera."); return
    pj = current_project()
    mp = pj.GetMediaPool()
    if SET["copy"]:
        dup = tl.DuplicateTimeline(tl.GetName() + "_PlutoniCamera")
        if dup:
            tl = dup
            pj.SetCurrentTimeline(tl)
    fps = tl_fps(tl)
    tl.AddTrack("video")
    nt = tl.GetTrackCount("video")
    tl.SetTrackName("video", nt, TRACK_NAME)
    items = {c: tl.GetItemListInTrack("video", c) or [] for c in SET["cams"]}
    placed, fails = 0, 0
    for n, (s, e, c) in enumerate(segs):
        if n % 10 == 0:
            set_status("Monto i cambi: %d di %d…" % (n + 1, len(segs)))
        for it in items.get(c, []):
            x, y = max(s, it.GetStart()), min(e, it.GetEnd())
            if y <= x:
                continue
            info = piece_of(it, x, y - x, fps, nt, 1)
            if not info:
                continue
            info.pop("_k", None)
            info["recordFrame"] = x
            try:
                got = mp.AppendToTimeline([info])
            except Exception:
                got = None
            if got:
                copy_look(it, got[0])
                placed += 1
            else:
                fails += 1
    t0, t1 = tl.GetStartFrame(), tl.GetEndFrame()
    whole = AN["a"] <= t0 and AN["b"] >= t1
    off = []
    if SET["disable_src"] and whole:                # le camere sotto non servono piu'
        for c in SET["cams"]:
            if tl.SetTrackEnable("video", c, False):
                off.append(c)
    tot = {c: 0 for c in SET["cams"]}
    for s, e, c in segs:
        tot[c] = tot.get(c, 0) + e - s
    all_f = max(1, sum(tot.values()))
    share = ", ".join("%s %d%%" % (tl.GetTrackName("video", c) or "V%d" % c, round(100 * v / all_f)) for c, v in tot.items())
    set_log("Fatto: %d inquadrature sulla traccia nuova «%s» (V%d) di «%s». Tempo per camera: %s.%s%s\n\n"
            "L'audio non è stato toccato. Per tornare indietro: «Togli i cambi»." % (
                len(segs), TRACK_NAME, nt, tl.GetName(), share,
                ("\nTracce camera spente: %s." % ", ".join("V%d" % c for c in off)) if off else "",
                (" %d pezzi non riusciti." % fails) if fails else ""))
    set_status("Cambi camera montati: %d inquadrature." % len(segs))
    on_refresh()


def on_undo(ev=None):
    """Toglie la traccia PlutoniCamera e riaccende le tracce camera."""
    tl = current_timeline()
    if not tl:
        return
    gone = 0
    for i in range(tl.GetTrackCount("video"), 0, -1):
        if tl.GetTrackName("video", i) == TRACK_NAME:
            items = tl.GetItemListInTrack("video", i) or []
            if items:
                tl.DeleteClips(items, False)
            tl.DeleteTrack("video", i)
            gone += 1
    for c in SET["cams"]:
        try:
            tl.SetTrackEnable("video", c, True)
        except Exception:
            pass
    set_log("Tolta la traccia dei cambi camera (%d) e riaccese le tracce camera." % gone if gone
            else "Non c'è nessuna traccia «%s» in questa timeline." % TRACK_NAME)
    on_refresh()


def _plan_json():
    if AN["sig"] == _sig():
        make_plan()
    return {"ver": PLAN["ver"], "a": AN["a"], "b": AN["b"], "fps": AN["fps"], "segs": PLAN["segs"],
            "trig": PLAN["trig"], "voice": [int(round(max(v, -99))) for v in AN["voice"][::5]]}


def _state():
    return {"set": SET, "log": STATE["log"], "status": STATE["status"], "busy": STATE["busy"],
            "quit": STATE["quit"], "src": SRC, "playhead": PLAYHEAD, "anver": AN["ver"],
            "fresh": AN["sig"] == _sig(), "ffmpeg": bool(FFMPEG)}


GETS = {"/api/plan": _plan_json}
CMDS = {"refresh": on_refresh, "analyze": on_analyze, "apply": on_apply, "undo": on_undo,
        "shuffle": on_shuffle, "playhead": on_playhead, "goto": on_goto, "quit": on_close}
QUIET = ("playhead", "goto", "shuffle")

PAGE_HTML = r"""@@PAGE@@"""


def setup():
    """Prepara PlutoniCamera: da solo o dentro PlutoniCut (la casa degli strumenti)."""
    _load_settings()
    set_log("1) Apri la timeline con le camere in sync (una per traccia video)   2) Scegli camere, "
            "priorità e quando cambiare   3) Analizza e guarda l'anteprima   4) Monta i cambi.")
    try:
        on_refresh()
    except Exception:
        pass


def main():
    setup()
    run_app((1180, 900), idle=_idle, quiet=QUIET)


main()

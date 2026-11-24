"""
NoSilence - plugin per DaVinci Resolve (Workspace > Scripts > Utility > NoSilence)
Fa parte di PlutoniCut, la casa degli strumenti Plutonic.

Toglie i silenzi tra le parole: ascolta le tracce audio scelte, trova dove la voce
scende sotto la soglia e crea una timeline NUOVA senza quei pezzi, video e audio
tagliati insieme e allineati al fotogramma (l'originale resta intatta).

Self-contained: gira con il Python interno di Resolve; serve ffmpeg.
"""

import json
import math
import os
import queue
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

APP = "NoSilence"

#@@CORE@@

#@@MEDIA@@

# --------------------------------------------------------------------------
# NoSilence
# --------------------------------------------------------------------------
PACES = {   # togli silenzi piu' lunghi di, tieni parlato piu' lungo di, margine prima, dopo (s)
    "calmo": (0.80, 0.30, 0.20, 0.25),
    "misurato": (0.50, 0.25, 0.12, 0.15),
    "cadenzato": (0.35, 0.20, 0.08, 0.10),
    "energico": (0.25, 0.15, 0.05, 0.07),
    "turbo": (0.15, 0.10, 0.03, 0.04),
}
MODES = ("remove", "keep", "mute", "gaps")
SET = {"look": load_theme(), "ontop": True, "tracks": [], "range": False, "from": "", "to": "",
       "thr": -38.0, "pace": "misurato", "min_sil": 0.50, "min_speech": 0.25, "pad_b": 0.12, "pad_a": 0.15,
       "mode": "remove", "trans": "none", "overlap": 0.15, "tlname": ""}
TITLES = ("NoSilence",)
SRC = {"name": "", "key": "", "fps": 24.0, "tracks": {"video": [], "audio": []}, "ok": False}
ENV = {"ver": 0, "env": [], "a": 0, "b": 0, "fps": 24.0, "tl": "", "sig": ""}


def _apply_settings(d, save=True):
    _apply_look(d)
    if isinstance(d.get("tracks"), list):
        SET["tracks"] = [int(x) for x in d["tracks"] if str(x).isdigit()]
    for k in ("range",):
        if k in d:
            SET[k] = bool(d[k])
    for k in ("from", "to", "tlname"):
        if isinstance(d.get(k), str):
            SET[k] = d[k].strip()[:80]
    for k, lo, hi in (("thr", -80, -5), ("min_sil", 0.05, 10), ("min_speech", 0.0, 10), ("pad_b", 0, 2),
                      ("pad_a", 0, 2), ("overlap", 0.02, 1.5)):
        try:
            if k in d:
                SET[k] = max(lo, min(hi, float(d[k])))
        except (TypeError, ValueError):
            pass
    if d.get("pace") in list(PACES) + ["custom"]:
        SET["pace"] = d["pace"]
    if d.get("mode") in MODES:
        SET["mode"] = d["mode"]
    if d.get("trans") in ("none", "j", "l"):
        SET["trans"] = d["trans"]
    if save:
        _save_settings()


def on_refresh(ev=None):
    tl = current_timeline()
    if not tl:
        SRC.update(name="", key="", ok=False, tracks={"video": [], "audio": []})
        set_status("Apri in Resolve la timeline da ripulire.")
        return None
    SRC.update(name=tl.GetName(), key=tl_key(tl), fps=tl_fps(tl), tracks=track_info(tl), ok=True)
    if not SET["tracks"] or any(t > len(SRC["tracks"]["audio"]) for t in SET["tracks"]):
        SET["tracks"] = [t["i"] for t in SRC["tracks"]["audio"] if t["n"]]
    set_status("Timeline «%s»: %d tracce audio." % (SRC["name"], len(SRC["tracks"]["audio"])))
    return tl


def _idle():
    tl = current_timeline()
    if (tl_key(tl) if tl else "") != SRC["key"]:
        on_refresh()


def _sig():
    return json.dumps([SRC["key"], SET["tracks"], SET["range"], SET["from"], SET["to"]])


def on_analyze(ev=None):
    """Ascolta le tracce scelte nel tratto scelto (livello ogni 10 ms)."""
    tl = on_refresh()
    if not tl:
        set_log("Apri in Resolve la timeline da ripulire."); return
    if not FFMPEG:
        set_log("ffmpeg non trovato: serve per ascoltare l'audio (metti ffmpeg in C:\\ffmpeg\\bin)."); return
    if not SET["tracks"]:
        set_log("Scegli almeno una traccia audio da ascoltare."); return
    a, b, err = section(tl, SET)
    if err:
        set_log(err); return
    envs = [track_envelope(tl, ti, a, b, lambda m: set_status("Ascolto l'audio: " + m)) for ti in SET["tracks"]]
    env = merge_envs(envs)
    ENV.update(ver=ENV["ver"] + 1, env=env, a=a, b=b, fps=tl_fps(tl), tl=tl.GetName(), sig=_sig())
    set_log("Ascoltate %d tracce audio di «%s» (%s). Sposta la soglia: l'anteprima si aggiorna "
            "subito. In rosso i silenzi che verranno tolti, in verde i margini." % (
                len(SET["tracks"]), tl.GetName(), fmt_dur((b - a) / ENV["fps"])))
    set_status("Analisi fatta: regola soglia e ritmo, poi «Togli i silenzi».")


def on_auto(ev=None):
    """Soglia stimata dal rumore di fondo e dal livello della voce."""
    if ENV["sig"] != _sig() or not ENV["env"]:
        on_analyze()
    if ENV["env"]:
        SET["thr"] = auto_threshold(ENV["env"])
        _save_settings()
        set_status("Soglia trovata: %d dB." % SET["thr"])


def fmt_dur(s):
    s = int(round(s))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60) if s >= 3600 else "%d:%02d" % (s // 60, s % 60)


def keep_ranges(env, S):
    """Tratti da tenere (indici di finestra): parlato + margini, uniti se si toccano."""
    sp = speech_ranges(env, S["thr"], S["min_sil"], S["min_speech"])
    pb, pa = int(round(S["pad_b"] * ENV_RATE)), int(round(S["pad_a"] * ENV_RATE))
    out = []
    for i0, i1 in sp:
        k0, k1 = max(0, i0 - pb), min(len(env), i1 + pa)
        if out and k0 <= out[-1][1]:
            out[-1][1] = max(out[-1][1], k1)
        else:
            out.append([k0, k1])
    return out


def on_apply(ev=None):
    """Timeline nuova senza i silenzi: video e audio tagliati insieme."""
    tl = on_refresh()
    if not tl:
        set_log("Apri in Resolve la timeline da ripulire."); return
    if ENV["sig"] != _sig() or not ENV["env"]:
        on_analyze()
        if not ENV["env"]:
            return
    fps, a, b = ENV["fps"], ENV["a"], ENV["b"]
    fr = lambda i: a + int(round(i / ENV_RATE * fps))
    keep = [(fr(i0), min(b, fr(i1))) for i0, i1 in keep_ranges(ENV["env"], SET)]
    if not keep:
        set_log("Con questa soglia è tutto silenzio: abbassa la soglia (es. %d dB)." % (SET["thr"] - 10)); return
    t0, t1 = tl.GetStartFrame(), tl.GetEndFrame()
    segs, mode = [], SET["mode"]
    if a > t0:
        segs.append((t0, a, {}))                     # prima del tratto scelto: com'era
    pos = a
    for k0, k1 in keep:
        if k0 > pos and mode in ("keep", "mute"):     # il silenzio resta (colorato / muto)
            segs.append((pos, k0, {"audio": mode != "mute", "color": "Orange" if mode == "keep" else None}))
        segs.append((k0, k1, {}))
        pos = k1
    if pos < b and mode in ("keep", "mute"):
        segs.append((pos, b, {"audio": mode != "mute", "color": "Orange" if mode == "keep" else None}))
    if b < t1:
        segs.append((b, t1, {}))
    ov = int(round(SET["overlap"] * fps))
    jl = (ov, 0) if SET["trans"] == "j" else (0, ov) if SET["trans"] == "l" else (0, 0)
    name = SET["tlname"] or tl.GetName() + "_NoSilence"
    set_status("Monto la timeline senza silenzi…")
    new_tl, err = rebuild(tl, segs, name, keep_gaps=(mode == "gaps"), jl=jl if mode == "remove" else (0, 0),
                          progress=lambda m: set_status("Monto: " + m))
    if not new_tl:
        set_log(err); return
    before = (b - a) / fps
    kept = sum(k1 - k0 for k0, k1 in keep) / fps
    what = {"remove": "tolti", "keep": "tagliati e lasciati (arancioni)", "mute": "messi muti",
            "gaps": "tolti lasciando lo spazio"}[mode]
    set_log("Creata «%s»: %d silenzi %s, %s su %s (%d%%). Durata del tratto: %s → %s.%s\n\n"
            "L'originale «%s» è intatta." % (
                new_tl.GetName(), max(0, len(keep) - 1), what, fmt_dur(before - kept), fmt_dur(before),
                round(100 * (before - kept) / max(before, 0.01)), fmt_dur(before),
                fmt_dur(kept if mode in ("remove",) else before), (" " + err) if err else "", tl.GetName()))
    set_status("Fatto: «%s»." % new_tl.GetName())


def _state():
    return {"set": SET, "log": STATE["log"], "status": STATE["status"], "busy": STATE["busy"],
            "quit": STATE["quit"], "src": SRC, "playhead": PLAYHEAD, "envver": ENV["ver"],
            "fresh": ENV["sig"] == _sig(), "paces": PACES, "ffmpeg": bool(FFMPEG)}


GETS = {"/api/env": lambda: {"ver": ENV["ver"], "a": ENV["a"], "b": ENV["b"], "fps": ENV["fps"], "rate": ENV_RATE,
                              "env": [int(round(max(v, -99))) for v in ENV["env"]]}}
CMDS = {"refresh": on_refresh, "analyze": on_analyze, "auto": on_auto, "apply": on_apply,
        "playhead": on_playhead, "goto": on_goto, "quit": on_close}
QUIET = ("playhead", "goto")

PAGE_HTML = r"""@@PAGE@@"""


def setup():
    """Prepara NoSilence: da solo o dentro PlutoniCut (la casa degli strumenti)."""
    _load_settings()
    set_log("1) Apri la timeline e scegli le tracce audio da ascoltare   2) Analizza   "
            "3) Regola soglia e ritmo guardando l'anteprima   4) Togli i silenzi: nasce una timeline nuova.")
    try:
        on_refresh()
    except Exception:
        pass


def main():
    setup()
    run_app((1180, 900), idle=_idle, quiet=QUIET)


main()

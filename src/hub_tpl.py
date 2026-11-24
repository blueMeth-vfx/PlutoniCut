"""
PlutoniCut - la casa degli strumenti Plutonic per DaVinci Resolve
(Workspace > Scripts > Utility > PlutoniCut)

Una sola finestra con tutti gli strumenti, come AutoCut: a sinistra l'elenco
(BeaTonic, PlutonicEdit, SubFinder...), a destra lo strumento aperto. Si passa
dall'uno all'altro senza perdere niente: ogni strumento resta com'era.

Gli strumenti sono gli stessi plugin di sempre, inclusi qui dentro da build.py:
per aggiungerne uno nuovo basta metterlo nell'elenco TOOLS di build.py.

Self-contained: gira con il Python interno di Resolve, zero dipendenze esterne.
"""

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zlib

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                      # Python < 3.7
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

# --------------------------------------------------------------------------
# Connessione a Resolve
# --------------------------------------------------------------------------
try:
    resolve  # iniettata quando si lancia da Workspace > Scripts
except NameError:
    import DaVinciResolveScript as _dvr
    resolve = _dvr.scriptapp("Resolve")

APP = "PlutoniCut"
VERSION = "@@VERSION@@"

#@@COMMON@@

# --------------------------------------------------------------------------
# Gli strumenti (sorgenti incluse da build.py, compresse)
# --------------------------------------------------------------------------
TOOLS = @@TOOLS@@
_SRC = @@SOURCES@@

_SET_FILE = os.path.join(_PL, "plutonicut_casa.json")   # (plutonicut.json era del vecchio strumento)
SET = {"look": load_theme(), "ontop": True, "tool": "home", "seen": []}
TITLES = ("PlutoniCut", "BeaTonic · Marker")   # finestre da tenere davanti
MODS = {}                                    # id -> strumento caricato (il suo spazio di nomi)
ERRS = {}                                    # id -> errore di caricamento


def _apply_settings(d, save=True):
    """Impostazioni della casa; aspetto e 'sempre davanti' valgono per tutti gli strumenti."""
    before = (SET["look"], SET.get("ontop", True))
    _apply_look(d)
    if d.get("tool") in ["home"] + [t["id"] for t in TOOLS]:
        SET["tool"] = d["tool"]
    if isinstance(d.get("seen"), list):          # schede iniziali da non mostrare piu'
        SET["seen"] = [x for x in d["seen"] if x in [t["id"] for t in TOOLS]]
    if (SET["look"], SET["ontop"]) != before:
        for ns in MODS.values():
            try:
                ns["_apply_settings"]({"look": SET["look"], "ontop": SET["ontop"]})
            except Exception:
                pass
    if save:
        _save_settings()


def _load_tool(t):
    """Carica uno strumento nel suo spazio di nomi (come se fosse lanciato da solo, ma senza
    aprire la sua finestra) e avvia il suo piccolo server: la casa lo mostra al suo interno."""
    src = zlib.decompress(base64.b64decode(_SRC[t["id"]])).decode("utf-8").rstrip()
    if not src.endswith("main()"):
        raise RuntimeError("sorgente inattesa")
    ns = {"__name__": "plutonic_" + t["id"], "resolve": resolve}
    exec(compile(src[:-len("main()")], "<%s>" % t["name"], "exec"), ns)
    ns["setup"]()
    ns["_apply_settings"]({"look": SET["look"], "ontop": SET["ontop"]})
    ns["start_server"]()
    return ns


def _run(ns, cmd, arg, quiet):
    """Esegue un comando di uno strumento (uno alla volta: l'API di Resolve e' una sola)."""
    st = ns["STATE"]
    st["busy"] = "" if cmd in quiet else cmd
    try:
        ns["CMDS"][cmd](arg)
    except Exception as e:              # mai finestre bloccate: l'errore va nel log
        ns["set_log"]("ERRORE durante '%s':\n%s" % (cmd, traceback.format_exc()[-1800:]))
        ns["set_status"]("Errore: %s" % e)
    finally:
        st["busy"] = ""


def _state():
    tools = []
    for t in TOOLS:
        ns = MODS.get(t["id"])
        st = ns["STATE"] if ns else {}
        tools.append(dict(t, port=ns["SERVER"]["port"] if ns else None, busy=st.get("busy", ""),
                          status=st.get("status", ""), error=ERRS.get(t["id"], "")))
    return {"set": SET, "tools": tools, "version": VERSION, "status": STATE["status"],
            "busy": STATE["busy"], "quit": STATE["quit"], "log": STATE["log"]}


GETS = {}
CMDS = {"quit": on_close}

PAGE_HTML = r"""@@PAGE@@"""


def main():
    if not os.path.exists(_SET_FILE):            # la prima volta: l'aspetto che usavi in BeaTonic
        try:
            with open(os.path.join(_PL, "beatonic.json"), encoding="utf-8") as f:
                _apply_look({"look": json.load(f).get("look")})
        except Exception:
            pass
    _load_settings()
    for t in TOOLS:
        try:
            MODS[t["id"]] = _load_tool(t)
        except Exception:
            ERRS[t["id"]] = traceback.format_exc()[-1500:]
    set_status("Pronto: %d strumenti." % len(MODS))
    os.makedirs(WEB, exist_ok=True)
    port = start_server()
    print("PlutoniCut: finestra su http://127.0.0.1:%d/" % port)
    _open_window(_url(), (1360, 900))
    _bring_front(TITLES[0], SET["ontop"])
    started = last_idle = time.time()
    while not STATE["quit"]:
        did = False
        try:                                    # comandi della casa (es. Chiudi)
            cmd, arg = CMDQ.get_nowait()
            if cmd in CMDS:
                CMDS[cmd](arg)
            did = True
        except queue.Empty:
            pass
        for t in TOOLS:                         # comandi degli strumenti, uno per giro
            ns = MODS.get(t["id"])
            if not ns:
                continue
            try:
                cmd, arg = ns["CMDQ"].get_nowait()
            except queue.Empty:
                continue
            did = True
            if cmd == "quit":                   # dentro la casa si chiude solo la casa
                continue
            if cmd in ns["CMDS"]:
                _run(ns, cmd, arg, ns.get("QUIET", ()))
        if time.time() - last_idle > 2.0:
            last_idle = time.time()
            if SET["ontop"]:
                _pin_windows(TITLES, True)       # sempre davanti, anche le finestre nuove
            for ns in MODS.values():
                if "_idle" in ns:
                    try:
                        ns["_idle"]()            # es. un'altra timeline aperta in Resolve
                    except Exception:
                        pass
        pings = [STATE["pinged"]] + [ns["STATE"]["pinged"] for ns in MODS.values()]
        last = max(pings) or started
        if time.time() - last > (25 if max(pings) else 180):
            break                                # nessuna finestra aperta da un po': fine
        if not did:
            time.sleep(0.04)
    time.sleep(1.0)                              # la finestra fa in tempo a vedere "chiuso"
    for ns in list(MODS.values()) + [globals()]:
        try:
            ns["SERVER"]["srv"].shutdown()
        except Exception:
            pass


main()

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
VERSION = "1.0"

# --------------------------------------------------------------------------
# Finestra a parte (Chrome/Edge in modalita' "app", oppure il browser predefinito)
# servita da un piccolo server DENTRO il plugin, SOLO su questo computer.
# Gli script Utility girano in un processo a parte (fuscript), quindi il plugin
# puo' tenere il suo ciclo: la finestra manda i comandi, il plugin li esegue.
# --------------------------------------------------------------------------
_LOGO = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design",
                     "DaVinci Resolve", "Support", "Fusion", "Scripts", "Utility",
                     "plutonic_logo.png")
_NOWIN = 0x08000000 if os.name == "nt" else 0   # niente console che lampeggia
_PL = os.path.join(os.environ.get("APPDATA", ""), "Plutonic")
_THEME_FILE = os.path.join(_PL, "theme.txt")     # condiviso con BeaTonic
_SET_FILE = os.path.join(_PL, APP.lower() + ".json")
WEB = os.path.join(tempfile.gettempdir(), "Plutonic", APP)
LOOKS = ("studio", "labs", "notte")

STATE = {"log": "", "status": "Pronto.", "busy": "", "quit": False, "pinged": 0.0}
CMDQ = queue.Queue()      # comandi dalla finestra, eseguiti dal plugin uno alla volta
SERVER = {"srv": None, "port": None}


def load_theme():
    try:
        with open(_THEME_FILE, "r") as f:
            t = f.read().strip()
        return t if t in ("studio", "labs") else "studio"
    except Exception:
        return "studio"


def save_theme(name):
    try:
        os.makedirs(_PL, exist_ok=True)
        with open(_THEME_FILE, "w") as f:
            f.write(name)
    except Exception:
        pass


def _save_settings():
    try:
        os.makedirs(_PL, exist_ok=True)
        with open(_SET_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(SET, f)
        os.replace(_SET_FILE + ".tmp", _SET_FILE)
    except Exception:
        pass


def _app_windows(title):
    """Finestre di Chrome/Edge visibili con quel titolo (solo Windows)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []

    def cb(h, _l):
        n = u.GetWindowTextLengthW(h)
        if n and u.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if b.value == title:
                c = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(h, c, 64)
                if c.value.startswith("Chrome_WidgetWin"):
                    found.append(h)
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return found


def _pin_windows(titles, on, front=False):
    """Tiene le finestre del plugin SEMPRE DAVANTI (anche quando clicchi in Resolve).
    Tocca solo le finestre che non lo sono gia' (cosi' non rimescola l'ordine);
    front=True le porta comunque in cima, nell'ordine di 'titles' (l'ultima sopra)."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        flags = 0x0001 | 0x0002 | 0x0010          # niente spostamenti, non ruba il fuoco
        n = 0
        for title in titles:
            for h in _app_windows(title):
                top = bool(u.GetWindowLongW(h, -20) & 0x8)   # WS_EX_TOPMOST
                if on and (front or not top):
                    u.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, flags)
                elif not on and front:             # in cima una volta sola, poi normale
                    u.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, flags)
                    u.SetWindowPos(h, wintypes.HWND(-2), 0, 0, 0, 0, flags)
                elif not on and top:
                    u.SetWindowPos(h, wintypes.HWND(-2), 0, 0, 0, 0, flags)
                n += 1
        return n
    except Exception:
        return 0


def _bring_front(title, on, wait=10.0):
    """Appena la finestra appare, la porta sopra le altre del plugin (in un thread)."""
    def run():
        end = time.time() + wait
        while time.time() < end:
            if _pin_windows((title,), on, front=True):
                return
            time.sleep(0.2)
    threading.Thread(target=run, daemon=True).start()


def _apply_look(d):
    """Impostazioni comuni: aspetto e "sempre davanti"."""
    if d.get("look") in LOOKS and d["look"] != SET["look"]:
        SET["look"] = d["look"]
        save_theme("labs" if SET["look"] == "labs" else "studio")
    if "ontop" in d and bool(d["ontop"]) != SET.get("ontop", True):
        SET["ontop"] = bool(d["ontop"])
        _pin_windows(TITLES, SET["ontop"])


def _load_settings():
    try:
        with open(_SET_FILE, "r", encoding="utf-8") as f:
            _apply_settings(json.load(f), save=False)
    except Exception:
        pass


def set_status(m):
    STATE["status"] = m


def set_log(m):
    STATE["log"] = m


class _Handler(BaseHTTPRequestHandler):
    """Server SOLO su 127.0.0.1: la pagina (/), lo stato (/api/state) e i comandi."""

    def log_message(self, *a):
        pass

    def _send(self, data, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        STATE["pinged"] = time.time()
        p = self.path.split("?")[0]
        try:
            if p == "/":
                self._send(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif p == "/api/state":
                self._send(json.dumps(_state()).encode("utf-8"))
            elif p in GETS:
                out = GETS[p]()
                if isinstance(out, tuple):              # (byte, tipo): es. un'immagine
                    self._send(out[0], out[1])
                else:
                    self._send(json.dumps(out).encode("utf-8"))
            elif p in ("/logo", "/favicon.ico"):
                with open(_LOGO, "rb") as f:
                    self._send(f.read(), "image/png")
            else:
                self._send(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send(str(e).encode("utf-8"), "text/plain", 500)
            except Exception:
                pass

    def do_POST(self):
        STATE["pinged"] = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8", "replace") or "{}")
        except Exception:
            d = {}
        p = self.path.split("?")[0]
        if p == "/api/set":
            _apply_settings(d)
        elif p == "/api/cmd":
            _apply_settings(d.get("set") or {})
            CMDQ.put((d.get("cmd"), d.get("arg")))
        else:
            return self._send(b"not found", "text/plain", 404)
        self._send(b"ok", "text/plain")


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    SERVER.update(srv=srv, port=srv.server_address[1])
    return SERVER["port"]


def _url(path=""):
    return "http://127.0.0.1:%d/%s" % (SERVER["port"], path)


def _open_window(url, size):
    """Finestra pulita (senza schede ne' barra) con Chrome o Edge; se mancano, il browser
    predefinito. Icona della finestra e della barra = logo Plutonic (favicon)."""
    flags = ["--app=" + url, "--new-window", "--no-first-run", "--no-default-browser-check",
             "--window-size=%d,%d" % size, "--user-data-dir=" + os.path.join(WEB, "profilo_browser")]
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                          os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands += [shutil.which(n) for n in ("google-chrome", "chromium", "microsoft-edge")]
    for c in cands:
        if c and os.path.exists(c):
            try:
                subprocess.Popen([c] + flags, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=_NOWIN)
                return True
            except Exception:
                pass
    import webbrowser
    return webbrowser.open(url)


def on_close(ev=None):
    STATE["quit"] = True


def run_app(size, idle=None, quiet=()):
    """Avvia il server, apre la finestra ed esegue i comandi finche' la finestra e'
    aperta: poi si chiude da solo. 'idle' gira ogni 2 s quando non c'e' niente da fare;
    i comandi in 'quiet' (istantanei) non accendono l'indicatore di lavoro."""
    os.makedirs(WEB, exist_ok=True)
    port = start_server()
    print("%s: finestra su http://127.0.0.1:%d/" % (APP, port))
    _open_window(_url(), size)
    _bring_front(TITLES[0], SET.get("ontop", True))
    started = last_idle = time.time()
    while not STATE["quit"]:
        try:
            cmd, arg = CMDQ.get(timeout=0.25)
        except queue.Empty:
            cmd, arg = None, None
        if cmd in CMDS:
            STATE["busy"] = "" if cmd in quiet else cmd
            try:
                CMDS[cmd](arg)
            except Exception as e:      # mai finestre bloccate: l'errore va nel log
                import traceback
                set_log("ERRORE durante '%s':\n%s" % (cmd, traceback.format_exc()[-1800:]))
                set_status("Errore: %s" % e)
            finally:
                STATE["busy"] = ""
        elif time.time() - last_idle > 2.0:
            last_idle = time.time()
            if SET.get("ontop", True):
                _pin_windows(TITLES, True)   # anche le finestre appena aperte
            try:
                if idle:
                    idle()
            except Exception:
                pass
        last = STATE["pinged"] or started
        if time.time() - last > (25 if STATE["pinged"] else 180):
            break                        # nessuna finestra aperta da un po': fine
    time.sleep(1.0)                      # la finestra fa in tempo a vedere "chiuso"
    try:
        SERVER["srv"].shutdown()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Gli strumenti (sorgenti incluse da build.py, compresse)
# --------------------------------------------------------------------------
TOOLS = [
    {
        "id": "nosilence",
        "name": "NoSilence",
        "tag": "togli i silenzi",
        "color": "linear-gradient(135deg, #ef4444, #f97316)",
        "desc": "Ascolta la voce e toglie le pause tra le parole: soglia, ritmo e margini regolabili con anteprima. Video e audio vengono tagliati insieme, in una timeline nuova.",
        "intro": "Toglie i silenzi tra le parole: dove la voce tace, NoSilence taglia audio E video insieme, senza toccare la timeline originale.",
        "steps": [
            "Apri la timeline in Resolve e scegli le tracce audio con la voce (e se vuoi solo un tratto).",
            "Premi Analizza: vedi la voce, la soglia del rumore e i silenzi che verranno tolti.",
            "Regola soglia (o «Trova da sola»), ritmo e margini guardando l'anteprima.",
            "Scegli cosa fare dei silenzi (rimuovi, taglia e lascia, muta…) e premi «Togli i silenzi»."
        ]
    },
    {
        "id": "plutonicamera",
        "name": "PlutoniCamera",
        "tag": "cambi camera automatici",
        "color": "linear-gradient(135deg, #0ea5e9, #6366f1)",
        "desc": "Più camere in sync (es. un'intervista): alterna gli angoli sulle pause, su chi parla e sui momenti di enfasi, con priorità e durata minima. Tocca solo il video.",
        "intro": "Monta da solo i cambi camera di un'intervista girata con più camere in sync: tu decidi le regole, PlutoniCamera alterna gli angoli. L'audio resta com'è.",
        "steps": [
            "Apri la timeline con le camere in sync, una per traccia video.",
            "Scegli le tracce camera, la loro priorità e (se c'è) il microfono di chi è inquadrato.",
            "Scegli quando cambiare (pause, chi parla, enfasi, tagli esistenti) e la durata minima.",
            "Premi «Monta i cambi»: i cambi arrivano su una traccia video nuova in cima, l'audio non si tocca."
        ]
    },
    {
        "id": "beatonic",
        "name": "BeaTonic",
        "tag": "montaggio a tempo di musica",
        "color": "linear-gradient(135deg, #7c3aed, #c026d3)",
        "desc": "Clip e canzone dal Media Pool: BeaTonic trova scene e beat e ti propone quattro montaggi a tempo, da ritoccare nell'editor dei marker o montare subito.",
        "intro": "Monta in automatico le tue clip a tempo di musica: ogni stacco cade sul beat, senza ripetere lo stesso pezzo di video.",
        "steps": [
            "Nel Media Pool seleziona le clip video e la canzone.",
            "Scegli durata (o canzone intera), ritmo, formato e ordine. Se vuoi, adatta la canzone alla durata: resta la stessa, solo più corta o più lunga.",
            "Premi Analizza: trovi quattro montaggi già pronti con le tue impostazioni.",
            "Aprili nell'editor dei marker per ritoccare i tagli, oppure Monta: nasce la timeline."
        ]
    },
    {
        "id": "plutonicedit",
        "name": "PlutonicEdit",
        "tag": "sgrossatura di interviste",
        "color": "linear-gradient(135deg, #F7A541, #EE3A5B)",
        "desc": "Scrivi gli in/out dei pezzi da tenere (o prendili dalla playhead) e PlutonicEdit costruisce la timeline montata, multicam e audio compresi. L'originale non si tocca.",
        "intro": "Tiene solo i pezzi dell'intervista che ti servono e taglia il resto, in una timeline nuova: l'originale resta intatta.",
        "steps": [
            "Apri in Resolve la timeline dell'intervista (anche con più camere e audio separato).",
            "Scrivi in e out dei pezzi da tenere: ◎ li prende dalla playhead, oppure trascina un cut.json.",
            "Premi Monta: nasce una timeline con solo quei pezzi, tutte le tracce allineate e un marker per ogni capitolo."
        ]
    },
    {
        "id": "subfinder",
        "name": "SubFinder",
        "tag": "cerca nei sottotitoli",
        "color": "linear-gradient(135deg, #14b8a6, #3b82f6)",
        "desc": "Scrivi una parola e la playhead salta al sottotitolo che la contiene; con Invio e le frecce scorri tutti i punti in cui compare.",
        "intro": "Trova una parola nei sottotitoli della timeline e porta lì la playhead: il «trova» per chi monta interviste e parlato.",
        "steps": [
            "Apri in Resolve una timeline con una traccia sottotitoli.",
            "Scrivi la parola: la playhead salta al primo sottotitolo che la contiene.",
            "Invio o ↓ per il successivo, ↑ per il precedente, clic su una riga per andarci."
        ]
    }
]
_SRC = {
    "nosilence": (
        "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"
        "o4rjPoL9zvjXnFXb/fOPrw6OA7Sl7az8f0nmftY="),
    "plutonicamera": (
        "eNrVvVtzG1mWLvbOX5GFGjUTJRAEQVKiQFF9WBJVpTO6sEVWqdscBiMBJMgsAkg0EqCoUiviPPnJcV7ssCP84nmwTzhi/OA4"
        "L77MW/c/6V/gn+DvW2vvzL0TCV6qao7DNdNUInPf99rrvtau1Worh8P5LB0nz6NRPI2CtWAynJ8n42AST4MX0Y/JuJcE7+Ms"
        "HV7FQfghnV5mk6gXB8+Co940mcwyPP0wS4bJ7BOevLbqKy+jYBJNZ3HQT/JP81kjGEZBL8qioB+fD5Mgm03no3g8y8v0misr"
        "aKKboJQMKsLrUTRLMBQOC/1O4ywORvMh3mkRjDj7NO4FYZw1g/l4NRnP4ulVks2ieifIeuwoXvnzPBr3U7baTaIp5jOJ5lnc"
        "CHoXCQc6jBpBPB5EWdIIZhGHFmdogUOrB3GA2sPYDimcTJN0msyi1cbKKEo4JZTMOKl5HFylQ531IGGj/fk0mkXBKBknowgt"
        "jaIs41M/Hg4xUjTcRwEUYjejeIa6yXBlmmSYYDRLg2yOKUXBbBr1ekkUXCX9OA3e/vDux33Ou4emmsHr1WjeT9JgnGIlkmCW"
        "9noR1vEoHg7Weul4FiXjuN8JzhMMHr/RQXD4aXbBJy7VOOVwzUbvBhkWLw4Gg9EkPpc1j7Ie5sRFG2pHzZUagGclGU3S6Sz4"
        "KUvH9hk7dWGf08w+/Xkez2P7Y8p9GNlf2cUcEJT/mncn07SHtczffMofZ/FogjXNG5pdTOOon4zP8xfJKF5ZmU0/dVYC/DeY"
        "pqPgYjabNGVG08AU+zbK4u+Pjw/fxxhXNvse4xnG00ZwbNvjxyOpshJf9+LJLHglNQ+m03TaCSr/+9qu6NNgs/n4F/XvdJtX"
        "z9LeZTzz6+fjfJNcvxqvSOHeEGBVNYPQL+32Utdl4n/9KB6l4zNd0SzYC46nc3cppwYJYJqAYwApQdqcqIwHYEzY7EdBBZKw"
        "a/gWh0dXUJo0kzFoxgCfVgHABWf9q6nX9Z68amZSIppMwpqpU6uvrOwfHqJAzUNBANCvg7Xf7D809hLHCAgLOMmgtvD5BTYp"
        "Xj/on8c8jaO0Hw2JF4IaRlhrBOlkMp/yQAfdafoR2xgAe/XjARZxltbRJHcWFbh0c+DdpIeTlgZmv18cvD1+/461FS83gqN3"
        "r98RIwjgAJmlo8kcB7iJlr4jNtXlsziZ5x1nGwNj23qu0mLwg7mWrzfQXjIGCsh7QnuTebqKIzeOdfzZHN0lvWHaIbob2JUY"
        "AQSAgDkSPAF35k0EgkLj83nc/G334ez1u+/eYbfTrDkBtmn+lCbjED/i8VUyTcfN83gW1gAQL/aP97EFtRrmV/t2GPUuR9F5"
        "0gteALGfj2uNlcpzXCvRPbZwNJ8QVvn4cp4l6VheKnTz0az30iYnhrSdDdPztDkZnwNkz96++/DqLabRum7ttOS/IBlwUmNA"
        "b7AHaB7PakE8BK1rCX7BuQOyJv7GwPDvBdBxRBx9nkQrZ4ev77skluByMMffH7w5OHv56vVBuRU0jLKzi3gUN2fXs1rdYDsM"
        "o5+AxqZCUL6No2O2tXJ2dHC8vB0MoTlMPwIr1YOHQa1JyoHuPxx8Wy5ucT2Hzud+gjruoKWx+srrd+/+8QiVw1o2I2Xi7IZR"
        "V7ZlnKIqscPR8f4xB/S5hg2odTB/fAV3MJtn/HWIJZqlTb7szrNPtgDOxAzPL6Mh2YTaBCg07uNFq9n6svL8zYs/oEUhbM0/"
        "8G9YD/KlkbOAMz10jkpDT0Mykw/2kMxxPqUYuYZo5ejg/Y8H72Wo2fQKnb1Nx9I5wU9/fVlZWQEGCYZp1D+TfQkNIs+xNf/7"
        "mMwugH5irHyxuWhpih0Eeh10PFidoctBk8g/rDcx2mQS1vMC0xjsyRhlAJ8zYpOFxa4rnNrXUtNg/QP5B2emU24vL63zyaKr"
        "2MyHJ6BqToCQUXQZAxYyhaf4GizaWXq5R3JVv3XuHyvnPmh+BC9ner1l6BNQWTPeMxlwBvAEYGThbzTe/PjweMxGpCEYNlnT"
        "XkoivlebzwZrO5UT4Wlq9uejSYhWGsGg7g5kGk+AAuPKHvJ395k+CNzZR1ANkLVwlsyGdsfAGRo6KUywSyGBLpIuUKWgDByd"
        "ITi2GeldmPHvB22t3iRz6bAIvdmnSZwVPJH+tl8xhuL7HHCsn5sc23DYBI8/3Wxr5XQ+7qPAyanyTJxGrxteYAGGDi80RpF5"
        "87t4puM5jq9nr+Px+eziQ3hRLCnOwjjAMUfRV5mW/JHTG8Yo5e9LtxhUD0dsFp/NgcHSfnzWnQ8GQIZj7MVG3atTGsAHjrLb"
        "CCpKYiDd5lU0nAvRkJ3oLNCh3m1DeLRVX6gkY3hOxpKsmwyh1wiqSmIMPR0DkAeYi4wQHdZ0788+JNh8TqZW71RSSNmYJgAq"
        "HvfdNTaIQnhRHdDBeD4yYBKa+YCKvvzh7fPjPx0ehBYWmt++e4fjlv/8/sPbF87P14f77/ff1MNeFySlVV9x+pKhWBAHyvdB"
        "PANHBy5sQIKxJ4ShAPpjkGdQ5Ngi/JgSnkXzRwdvDt8fBC/2f9x/e/wqCMEvX8SWge4NwfZBCkU5w3TUm9LqMeW4QM6G2y6r"
        "UtIjo5iCfoD+rwZhL82SVXk/hRhEiS2CuJZOgTXi+q49PRg3l5Pt8fRQHBzNIYTGVpgEjFEw1Xo8wKs6c/QwXKXMPeKIJhBX"
        "i2NasC1fKduygOtbi8hx8XDf5YDfesgVTo7s4TnEyKLpuTaIo78MJPSnPSJneF36Vc3alU7Z8urFL3Das9NivsPoPFMusNXa"
        "CP6iD23zsNFyBUzDAGaTFIdMlCYN3e95NyLfPZinvdRDY62in3SquIH7rDtaooIocMGPSxG7xy+kEzTfTdNh6GCq1+n4XNDE"
        "WrtVD36HKezUZeQfjs4O/nh2/O7wzbuj4yrskSouDQVCAwwFvBs7WYIw/A1mj95GhmsbPNjO/8syL+KteEg0jq5M/9J9pyTX"
        "m4MhWhjh0ngicU4maYK60xEUQ/+mg7xTW+1fNmEscefftEMQrL1go4wOxnfjEFsWEXenYH3OZHcUJBUPf4yS2d4G+PECC++T"
        "iESeiAp4hupKFI+K8gR9EQNGwxKaDlVUVjVIgeA4hul8HDrQGAsnQYVTk39EnuFwCp7uAtKLV+ApK3XKtNsjMmZydZfMCKu4"
        "uEm6RD4Tz86yYRxPwlazrXuR68iaqgcKQZ5BjPcwnYbR+mgPSrjB9Rfs3fATBNb0MuwXy0s9GAr+jN1KlHQkHXCiE3DBaUAB"
        "AKIalzS6ioCeai6J6KsUyhbBvWKhVXQjFPZP9PUp6Qd4UfuzmLXzEsuel8+/O8KDSiTs0au0Z0QVX1Kp29HVKAJOahxXX8Yk"
        "uA0d6fvTuhmaTkJfNoLS3kiHpoJFj04TeTlvz49fHb8+OGp4lfNdECHvRhmjQnYQKe9uIoPZ5rwHESHYaTgAEHJVDZtzd8EA"
        "bZ2pbB2OzIBF/j6xEjeXZuQUhkxeLkkx3RZTzeaZ0ZKG1crTAkJVtZkryjbaj5tAEM0NUVtNoAACdgjXMTlyT6JgD9ejSbLO"
        "55gq+FyXJcCbn38M6WxEHf95HGbQqzeCb6J6eQFsYSwoOFkt1oeq1JD/PWoFwexFXLx1UX7gC7jwvXar5TTGik02cQbl5wT6"
        "njhkqXpFgQucaaxK7TkgBxzB2jG6qZnu7lJeJRvUAKoMh4AiDrdev7FqBAZ0jQ1M06HqWdagjJzGtVKtolIWlj59FN2Oyt3S"
        "Y7F2/fTsu4NjWTz3aClgGCXMqY97i13Ae2lfFEkZ1hqH9fe1+kmrQBXe6TGnfyL4Yb22iGalNd3Ow/3vDs6+P37zuiknC5jG"
        "nCvqxyCjrV/MRsNdcOfRFGC9Zz6uLJBf01kOdDf3mgv1UCFI8bBeLw+gshNgMqzj0WLj6ZzaHn47mZyGlcIcBNkxOhtDX5DS"
        "ZjebT8gGli0eYffTDIR1lkxSfLTGtxHVrONqnsiZGBrGtjQ4nJON0ypWJYs7tzXirA7aufPChLV1qmIJvOuD6AqS8LiJP1Xy"
        "qYNfqXcmbu1WotKK0VmNGiqBgTyP11X1e/ssnTa6VGOqWFqzgAZNTkLUsdVyZHGDnMNvp+llPD5MJmpsaQQ4qeO4R5QD6TKe"
        "Lbzd74IrivvyvrQAgtVKHeTYn2tQGvvC2SpNhkgmri87P3Za261WaZGW0p1q9AsUcghB45fhEMpNYHWlctOgL6X6JZxZp5zi"
        "jHRh8uQQc4IK5k5anArmE7gY11WLJBxJt/b5S63e7Mfu0hDaVG+nvUmZlbstC3v//OVeqLGEnOJZ7WZuoe+MpYTZeqP+bZV1"
        "UdmLzO3zF3/TqWFvwroV2pJsErBifoGNrbl0avEgGSHil5wlrw74R7+w5c6EYz5TW53lzaC1p/W0wg4b1nJepEatUyPnaeq2"
        "ZrPaDHsTH89aMoIzCPDxFc3IlVy9sqc0LjTnkz7JCGqydkOEoqKd6VnU74PtyHKkbNZRa5+oKeLUMqjz6TAkRO3VLPK02n0a"
        "vjvr6wUD9qC//gD894Mg9JvCCNBAzvIS1xrmOETr4EySnxd1y+Dl5rS0QnUcj3+GTAeepA+1WLwadKMpnSmoY1YFZJAGVD/T"
        "sYH2yh5sog3HJCstO2bZZvAKddVBwxEjY/NCmsfWkIDkTivQXCgZKYRGq9k5qa2tAfT3apAQZT74PY4/rukUa/o7XRvASDBb"
        "g1iWv8F4Iijc1sww1zDD3mXZxoii2tAal2nvQb/xoM815i9piKqxNbJYazBDyCA8OxtMbzQvTVOgpfTMdFWrK07ASgkUnpyW"
        "9Xx7ZT0f1UddsOZCXcuWRxjYzqfR6CUwX8YjfNP38HrnUR2FqvVt5Zqv3z3ff20Nm/X6AmPHIVVow2VeUEyceGvBwliM79L0"
        "fCh2XwUfPu0XfDt/9uRDM76Ol47UDneh+TdJb5pm6UDMyYTLivZHWYwP2r6uveBX+MI0gYJmWOyRbEE/mmL3nW0oZlZbd9rM"
        "1nVS5kBQ1b5uqFm2/ibqvTvyC1Raskst5vOQw1XVpF+iZifi7kgxXvUDakJt0rsgZSRAjZVXO5ehrfXy3ZCnZD6SpbKdrHHJ"
        "7HKxdk80d+yg41K4noj3dmfEIgdbQv0ujEzunNQ8FI7wpHeK8yQHnQIU0POeU+bFwY9vf3j9+g6aY1QF91pVl5/i6fSXNSuW"
        "HuyVDHBPHQ7qSxRJBaW5J9PlqPI/xl0XpZqGi7dNWTTgQIvrQergUALpNr7ao2nbVwKI/f0090VaMVo48hGhorcE1HNPLeQo"
        "DGoY1h1N4NVVImpxJWsN6AHpwOZi9L5xUClkfn4Enl31y61KmxFc4WZRR/S+8HmCxWbep6pLDDTNYJWjWVUfu/Qc6rF2kFkL"
        "D7X0vVV8NSp8VBpAJ6lWmaJzwOuqTATGFnovwnFvHMP9kNXh/Qd2hOYeeg32eQxTtbAOo6t0muZ0xzU6C3avMDrLdu2VGBhD"
        "B8n71h5knWL+UKNUEXMh5cC9ykEYVswj38Id1A0F18+uJlc1XyIEVmvWDGPEUcZkZ6EJmp1xmStYd9W3kr/zwKeznD0HPwmg"
        "mJ6jMeE12T8b5FGE/nR7gdFWZ4+D0WS2vCWFRv71cM6oz81FNyV53IxV3E4I6rWaU1xAQaUDvLkdP7H5E5Q8DTGWmyUold2s"
        "HE8f1ty62B3S6jjD1+FqTJkQdvuIRkFyPIvKAuN5Ce/UuAsXqwrpTzV8tYP379/BBEp3WB6B1QfZauefxoYjlBXMG2mSwEWz"
        "M4w6rJ+sbcA/qnNar1e2bVSNNRFfMWhtMPYLY3JwsqlYssX19yUaV3+/5sDfs6DdbPnN3QScDijcpkO+UUcsZWXL1IDsWoUj"
        "tXwIlopvBxZqefrDJRoWfgnrv5Qc2NXAQpRFbpBlc5zd47G4yljgsL0t61VqQY4DIKLsYwFad7mMEMJ0CspJ+51jFCIyt26X"
        "6aqiu5WSGWUDpqVlTboUYiA+6PRTo3vlFUQJbEiNFCIz3lDeHlj5hy5ep01yPtjlcXg3Jftv7NN6jNkOEyItdSGP4T4AVwK4"
        "5dPIE9OvAFYegBeQ2hFQLHSCjeC/StOR+Ps1yl7/v62n5/v941fv3u6/Pnt5SCe/z+3N5pPHjzpBe4vukuvBBkzkjaD9BG87"
        "wWbLfbn1uPlku90Jtnact4vAvo26W53gkVd3Y+NJc2enA+OB89r63dHkezaAwhH/M0A4oPcclDwzUXDxvfVyzN2uanirnpfW"
        "va6eM6o4udP0moQ66ol/nTfvJnTko8y1PeJUwJIVDnBWtCptiyDLG5X6D2nVc3ExEwH09s/Uy9+2ngt809qLzj/pt3/qJmPz"
        "KAJJI5jWni//WNNfNVdk9ES2ly/fHB5856papdcmNM44kmHLFQ9PbOnT+o1s/QKGc/hlGm5PepSFweWI8y74jV40wVrEZ6D1"
        "UDEJZ/QLOfgqJrsRWD6iXdJkmi3o3VGHx9iNZGwYc1NXeIuVFV0YAp67jWZre/PpFIzmGSbyE5S8oa+aMR719Ns41AJvojHU"
        "04RXenxp5UNbt9TmzCAM2+jkJ7rjlDt0+5v85DR7nFcXdeNPitDNpNjPbHh2GX8KZ8Mqa6dVLj3I/mI4h9mQjf8wTsCdvRJd"
        "u76hy5o9ZLc6GrhVhCANjZm4VgyKR/7GQeWIQVs7UkVnWLML9hIqjvg9DT53HVZ7q2ldIAasnJ3N0rNZL5Qf8A8oEFBCY2l0"
        "HQJ5kX+fUsUpiMiiGaPUNh9Yv+7tUe1Bq93v+H9kfQfB+jr+JsE3wSawJNbXefMI5PEB/pqXeGd/PcAPCzmzHsetMwhnPXfg"
        "EFpWv/++8+ZN5+io8/Il5J40WN1dBRvwDId9JmohuN6A8KWkMxR3ZjnBItBQp0fxCM6HSb8QgkTbDVw86+X4tkDFu0RTnZrz"
        "prHwpmnfGE15p3AVoJV0Iv4AWws7ljP+HoDAe2YE9NKQbQi5D9eq4LgmLpt40PAjvSidMJmqxm/dblMhDC9kk6CkGNXtU8Yn"
        "tPAwJwTkuy/hpTZICwCnQyMjzmITcBYb9kB1oMUeUMs6po4V24BAvqlsERwaJ4U8KubGzzVpBz7sJ0C/NWlMfnyxyyoeX8OF"
        "SaN6jvovgepUJaSN5Q3VfUWkSNIQM2CqxwqZ88jpXD7HSs1CtmOsN+JPewfdj2o9A6cpwRZsCXugxpnaPRlm02apnpD7vKtX"
        "+PUa8vursXTqdXhyevu407F1RDHtZdLOwTiCp3I/b+6+vL40u6AyoimXLZ5aX97PtQSbnNBHAHPFo0yZa/UgUz21jAAqAFgl"
        "JrGEeCS0B0KnKedMFoMvUr5Jx188+CZsWD8SMWVikrB/FCB8qAGoUOHA9yedRhQQZ3NErwUOEMM80I/WYUngC/GzaAbvYQeY"
        "0lMErhtdLHagknAO1BGQXLeV79IRZRrB7wUBOhj3zRsXwo31SoDTBVszI223YWECR1r6sMRH+26IR7mHVU2z9J618T1EB43K"
        "UrPULWNHBxEmU5RKVb79sXyEx2a1AlFcJZAWsEyUiBx8HIQOcodpxB0/8RceI5KUEdXjbDcfTTd4uhdEy3t/rdIX8BFCA6/i"
        "cUJdWDpR7RgHYzqz9cR7Xsj5r5dQENl2BTSYKkpcOXj7Ixl2BhhBTqiQFAuH8AglglG2csY6z/eff3+gRlpj+iLePAPrGw+h"
        "RROzWsPq6CSO+Mwhm6/NGOiw9i3JcamPOveFvowILlbCScOzImzDsFtgBrtFuqT9KS3Je92s21fSP1/ke8R6lFbyySxsV/Hp"
        "BIUVXY2raJc0Drpk19ISMiUeUAG1IN+cosDYPUyGBzbuwiWVvhgVl5ITn4Ejt1CSGLRtERt4WuT8i2kuEznuQXNzQPzFmWBo"
        "+T4RV63N3BK6bjdq62trSU2toNKfmgAjxrjVNvRZuqYkKz8HEr+28UitVGsQaG6UUO4l61QLNDe1b2UdLgWZP7uXbYeqTKOP"
        "WGNE74ql427cry34ATV3WsFd/vs6EGn/+59VZl8adABWZfKJOtDxJP9Em+540iQKNTExGPRJh1QID3Xyt23OCuxLX535niZt"
        "MIjQVuFXyKpUAbjhMSOCOlAbXG3ZTCSNfPBiiEYlmV1IP9rK/gxZOIxOOiP0+eEUjGl2AZ1VCD7yAxg4rm5zFEdoFNC+t1FH"
        "08Fm+/GjnWbL9wIhqm23eHImUEqcb7Q4UmxVMpqPQnQHbUe8tl1iAEjIOyMqRE9Ur+Hyq/3uqbuBbmh8eZ1pKv9UfskUARLh"
        "PfLWXso25W9Yu3DcXCLdE7iaZcu2xFtSWklZGiEscASlmbSbnJecUSIpkX2M3DjHEutYsXMlThHGIw78hKLQh06YCDMp+1Wx"
        "qWcj3dQM636NQtfFiqId7t+HZZvI7UhO7UaiId1IHrjyDpYwLsNq57NqnknZ/pzckHViRImSyglN8sALrl3OoTpGDDA5KVQ0"
        "QKDQqtgJDMk5Ud7JEdmqyFThL1HF61hnrLBLZRfXh8UcOsElV2gcX1VSCsNNnyQz3V8NYK1mro00wXUwDLZoA5KZ4eegHHgG"
        "WKX9WN8dqXcN1G/dwu582TC9xCIR0ddGRtHwJA0qP8wal3zuzFvwaWZ59x8gdwd5g+BBn8tpOGdu1mXDYZOdQzyaUETUQb6B"
        "LTA6hAjA6brOsjyHeyyqcX7JBCofqsVx/uiKERxG4uVGLIXmrELEDYAE1WUrNyit5JhAtZmPxixZw13VvOQ1yMf1hmEQUC0y"
        "vGFMSFpxHE/ZUDqf9mJp7lhNB32EwSXDYJYzpgKkmrNFI6UkBCRLVLNNUCVP5Lh9TXstkdB1pEUHhpE3IuO6bIbAKRbG4aJ1"
        "VcW0Ws3Lsf2HQXjdAjRnBpohoGLKa5i7eeEgs5anttF6VafAx2E/NYIrH/44qEXnmBZ57IQj+gkQrAFBV4BwjPpEX58uSn7O"
        "RwzuykUt+GRQC7o9jzn7LOQf07WBGL5ZIPZGlrW/uP8QITIlO3zihH6GHucbadD6nzE5kLjLZRfpsM/OnEgAeI0k4sYFAzRO"
        "CSWUjPGLNOLTagNYQfIhY0UfpDTUS6qLoYfkrsAf5UiKOCYTd9nQwd/EPZjcNdZu7cm2pyi6InJotxbmu7bVMkgeBJb+uFdp"
        "0uMpwepiw03Vb6DW32jVwXIsvH7SqntLpiDCdVt7LAwpz83aRrspsVnoAlvWam5uonKofa3p+zrxhpGiJ3HcuzgT8ic716Df"
        "oTR1hiU0D1LIUxPNmNPAZFFiQIX4JyQqcxgLGWAGRx58SseEX2WyO+R64aGGxCfzVSAOTRclLZs+4d9XhxiTiJ+59pSXlR6K"
        "IUnRrAdQB/+a7xiYeRwxRkqKI5vEqnEZCSqO40DCE+BhZwH0U2wplqAUZ89T6b3RNopTxLo/VdbV+l5MnB2lVZqcYLA/lRzi"
        "ic1/usHXNSmaPI8mwDQX6uFRIA+7ni7WaJS/6zouIBY5zv3CEZCAPxVGCcP2Fs2U5BpModUBjOkb0OVTOJRigTA8f+h+AQgJ"
        "+OeGmWpxu1ZTTx90Mi2GZkaCMbFBDETG82xPl8biD1hsL5DyKzuDUw8c1xTmmb+IcVMbze0CzN+kmqYMIKdpwjqSrQdh2wWa"
        "CMIRaS0dZmCMFCFcgd3kz9nZ/uv/zoRaPbY0LLAKQxnp3mLAEozCdo4wmU8KO/JxGcbMIBGATbDAbZAKXJ2oaz4hL5RvmSEK"
        "V0VTrPhQqS3mvbbDUMqCkqENu84oWFjgHB557FCVIbxqL6xmmqcdi/7REPAxfyJC0k9fkI1s+yE7O7lIuFHyOExPSefCC7Yy"
        "TA0gCuLqV+LgbJSj4EctT+WmlZatH04osa6UKXCs/FREu7NtEC2S/TSsXwPXem0jfrJSrXIGfi8ze9lIsMmedMjzkb/gA8uf"
        "uq/17UP7NkyMYwQXJVc4sbLCaomFnOXIJKkvOKnk2Mua52bZb6MWm8Q//5xY/f8KbdeLOq4FzunMNTkInxWQZbHMG7neMCls"
        "QQm3/lzj4CWPyIwZrkw+EUShFfKEJ/erisuwuoWx8BZFhNZK+hysp/UiWFHzlU/xBlM0z2FuGqxmtg+P3FClm7XvAy+svxiB"
        "FfcGHOMAALShHLsusbvfpTqW9k+Rngqgr6piSBbOrlQsPtMCrmrZVXd3PGnvkuMBpyq7qEyr/WgyXNjkkhD70QZTibHhfjKQ"
        "gOJhZLMnStICrdmhXxw3368T9yS7WUH3dbKOppF6h2YvToZmjjjdlzx38dqjggWaJHEPqrIBZACIIo3gRUN5dApbIwmqLGKh"
        "C0kXBa8fvvBlXaapTE0OqHzZHKheTWarTLbhAreJYGKZ569fHcL5Bk5ekogNkbjgU6HPHThHJKFHg+2jWFm7BLcJgVmrkMoW"
        "ZB0L7mzEMQygzjLLQG6e5LYvSkgKANqxb47OVCjCXngiItHvpdU2wzgpVsSROw+YhKT5WpYPHK+yAfW1xvoiv2n1NOD9gpDN"
        "cB4K/K/G/fgaJbi5tbNLPF3m5kjZbAeDYwAn2ruE2Ep0Mou4wMZCgKPj9/tvj16+e//m7B8P/qTJyQ4jUeoeJ0PRDdN/6o/2"
        "4U/24TuQDz6/T2eigd0fmziEfTj7pdNDsih/LP3+U7Wr/mEy6zGqt/anSEJMXmI7/mgfpMPnQD+vY41C4PP75Pwi/3Esnony"
        "+C2SqNGoVNULvx/B9Z7edXk78UwDqmrvSJqQFS93IEknJqEAN4Pnq0+PyZmjXWI6mgllNjFdkJ6k9FbRNDWxpLNJGMENv8UA"
        "J6ZPszN2MhN/UUlKUI3+tcPmc4zgO1bLwhPt+/ROPncL7UFHI9oq06460ig2N7FsnkB+SfzlQ8WCMC5lpN1FgUHHyowceTeX"
        "DS18cnl6j9h8xB0V/jsjyAluprWhUMjmc8kPJV7Oua9OkRqNZ7vtyE2oZbED+ZRLsKvQwDnk8IZWYQQOwgf9umizxuLScunQ"
        "wctCpsnddGwYQNydJ5D3uf7UW0KBmulk0AQcN88gZWR7JnPfT8M9sqO0My7RatoRmbS6kiHXSpurbHu15PeQI/SGmJPFEXfm"
        "uUuopVP0osDOqEKEjbR/jlOLSQIlxCC6C15fMTpn+OafhKqm/Zx7UtDGz9AYxCJMifRm07+Y3IGjaHoZ811IRw0qwhBvLx+/"
        "1E+bxgJo1qyjoQuGrunRi1R4YeIPZmqb4KAFV/MUa8M0hvKWOpapsl/S3k/DDqfHpM0TSAVMlTztp3XNeayrkoxdtiGUlMtp"
        "8O9BJczjawYOzM2yesvJjMWR4UEmKX5JIqyracQsnY6R/gbPtBEdhNQvLaeM5pOnh1YAM7roK4KYOfULPiXGJUV1hI0lZYy3"
        "ipRRgSbXTwvDQb2zZmryxYkrtSw4Cuyi/QUltvWNKZTYKr9Ed+sruk9fiwrzU0/00i6JGfhTR1DJOKhJPz9fVNZeRMaRSNFT"
        "y1v5BVZFUBuRTTWOy0elxW7iYTAUSXMjuQpjsYdq4FAxunl6FTUtkBEvl50btRvtFfqts584svCnIXUQ1NAOGV2br0t+/PRN"
        "VJHlRZCtafYGyNugutHwvLkuNPc30tr7/b4HKU7oexz3z8QOGlFN2OaCmdGr2339bgNywFwGpO3eMpjIScizRKq+qvDWMu0c"
        "uV5Z+RFIFg5i6Xv9xv6iu/Znj8Gy/vLv9TvzG+PMwnI1vBsSeKZI0fh46QHTAl3y1sKkxBbr6ZlG4Dmnac7mw4VTqRWbCAMP"
        "fX58eQmx4uh34NuM+mzxo4Rv2SBKhpLgThOF5eOARsjQsBQZqH1zBWlctalMDwj4BRgPYdltLTOdKevomcskdabqfqX9umc0"
        "LvsbVRqyeONBrnv6zNF/QaK+z44Wyiy6uFM9xSsYBp20BhLjbYLEPLiD/PjJGjhEzYQ/JxuIefIH9AJ1P9FU5L29mghOpxBz"
        "k/S6IbpQD2o0uXS9KuWcBRWlDgZWcltobvfcwxwLc6gxkWKQp4sjNMt2MpG2JQ0L3nNvgQtPybd92kM+6y7leehWgSn9cUWT"
        "QgHtbJw/IwntncxMighz6pZEWGmDhhbevHZwZSmtiB6aJW4xv3SdilE5ixSZRWqfLsxdSOykIK8VufwWYFhxB/XF48yK3bNW"
        "XTVblgwJYMh3Pc7ljq8mndtdccDewuWyYzhi32NAbDgIiM+ixQUQ4ZY6WcKAawzwSzQnKe4AOGNyDuHmqyLcCr62cqe0oRPk"
        "WMFIjb5ATBBxbzHjZZV3rdhb0pmKOJJ6sH+c5kzAibZfldvoVgfbomUPWZQ2HyWWZNQVrLtgZroRKm6cJZZEPLswIHRKh13X"
        "ll+8ekF9HT1xf+mktY8XC99GlPZxMrSrRaBIkf1vKtztlZydM2MEBxQBkZ4uJl26gzOZZN7NxRVz54LKRiv3GXfDGR0wXQmJ"
        "CWKhfgt9aLmKaE1RP90FTrVkrv46FU0bXzk6slNHIYZxN6REfp4q6MFy4F8G+mzxtDqXqewkQPO8wof97nDiRZv6FKuRL2cV"
        "Rrw0K1LMdzGSdUpQU24dJy00PDDx7DXZjsiwIc/oDaJEdLGNVEi9cPrlRj6xka5p5GnBkojVaGmDMiga0ybMZmqMbIta0/qy"
        "oUhVPOZ1XWK0FnyqqFkBOWuu5pZj+sZT2vwKMMQvtvcw0EFe1u9+CtRqWIUlnVUn29heEu8sDTs6YU0OhrFABjDIQK1bqn1I"
        "OvYmJBoirO5hHP//8uAwRtOyTKo+qmCWeLpGYlfHIKobXjpXGbtIyRDnn7OHEB2emM6WTPZuk1k2IeW0F6T8MiOuoDP2qyvL"
        "Y74+FMS+UEalG5/Aut2SOOdK0Xx1jSbuTvFGuXj8RipJ1Jg0T+vFn+if9FHuF8I6mmZPNZ2i92YDbzYk2OOXBvPb4EYZD2Q2"
        "zVBh5q8+gW8luN6o60c0yfI2FbpmhjXKYHpyNDM9rhxK6J3DAEHhUbhq8mD8C+u/kS16GH1i8r6Vw9f7f/r+YP+F3qkS/5kX"
        "uDBKOYmHfXvTy6wnT1+KtDS2eijlfJuf715oSzo3qNl8GfA8REDKKt0S8U+0WsooVpgqho6esgivXRYY52a9eGvyGuSqKc1r"
        "0KyVL1GQn3Yt8hRw8Z/37LsTWZxTFZJl1nvyV8FaHhh4ZyOMZml+6Yu+qkYJvT3jausoyCTBoYmYq4jKdJIDgddLGcJZxHNJ"
        "/u4lqy4X5RXbE5pkvwd9mJjut9Z5Avpep2oZ/YiCgRtPbWOnmSW4bt0EwhpUzGJb681n5dNvK78DIZBqWvjudqlhSf8oy2ti"
        "X+u/eboIL93Db9s4DGTP//Hs7f6bg4qr3JA2xVzglNJO615+1LDJuzuBxraY6Lri6iaBUHvWU32yK1nDKc1sWCrvdcTz5y+a"
        "WMw+ijuQlsmrzc6wmVnRJywM9GR0X8CdLlLLS/6K/mb5wPK2pObZiJnc4HD0SDqHN95Fyjuf2s1tMeJc2xfq01kTYiDYLG+n"
        "n2QM6zwDU1V0SrOrsxZZLJdYIZ2FJpVR87S32I36ytH757LcJmxTVg4KGvvIZBYdCU+3pnT+Xh7jyy26dAaBPFb5/nxZ2X+r"
        "AcKyVGwxS/JbuiLzqmvxttuzsy+yX5ndsF6qjekP3SeGGgPZLfQFxts2gUDxc1PSy4Jf5Cw1OdEdjZKfKH/FM/mGClz5SH2d"
        "ppNkWQMz6ZgwhHWlhCGYGP5SYnHKkeN9vqdvGU77db2ZZH1YxyEDnJaHIXBtYPoOg4D37tJBfGZnvFeVg7nSwURG1uZ4bLoS"
        "O64r5ELRCHwEqjlj/FIeox7aRn6ynDPlnabiHDV8iLew7k9QWu8vmYzJ0H95urBxLp27fcEwwWXrJWtiUhCcdNotL07EuEyy"
        "x9DBAvTjbCMPcp38Vo4K+BaoYLOlry1CEAYPmWrc4KSqHOdVC+GNlFIhB0Q58SJpmIw2sjqL3mkhvWBMKukiZcGy9NHuwgra"
        "soioaq/UDRKgI10XELNkgQmHxRYmepFcUbZ8TVvBXcDNlHEDpeSD92TGgCgtN0VcuUekRU02/0XKPYPwFEXu3YQgiRLduxbK"
        "nN4+zrBzQ5Rnj6TDgr2dOLa3IS/ygG52Csq3XtqHlarZuHlRdF5FdhZRj+8V8Vr5LGdTmbqQnzK0ySyjMRo4UdoiN3M4lF8s"
        "0uLuB72srpbN/IZfokFD1QT1qkpK7ha0XFOv2aKTr9xOTt0AjZNekVVIruAQlH2qmULF9NHPTiWIo3TPhym3JyVc8JCvivDF"
        "2MvpMqyGvXGsuu2n9cIn261Rvk7ENrRk4qaxYuKnJx2jMnehJ/dw+eu/PMj++q8dimWuw0qDL/RqZ83Q/P65XbtGPnC7HY2F"
        "qfhpR3I3nTPNI3engxUWkOVn4NGbVzgeMiBORJJzhPPk0eAcSsmOnBsCTopWzHUrSnPsL8H59gcQPx6N0crZ8Lr/0u7a0ghp"
        "U5gOW4ZaXjacXSdF9mjmVf3UwVHIyzT89HM5QSqzm+qV3Bp2gLTWzII74KU89EU6P2cyBCAJceYxl5bDncJczZD40pC3jjfI"
        "nJLF8ZegoV1XeDLHzl1R/3DlXR3Jxe3QuCHwIpWb1Q3E6oTKDasxGXH6YvIu0oGgp3xa+LrYEV76LVVGogpLiej4udiAyVj6"
        "dvUyAnEkx6lvuCw8a25x4OkZn5pS0PGdoyntpPOg1Yz26IqrpKZ5GEZV3GWFdjfnQFk4XnJJm2sEX66944KKJ8F10Y2sNZMr"
        "Qwc3XSk8U4yrg55PwxsKijVvhCuUF6F5Y5lGpQfExM6pkwsrs1xlLkeZ8+5ZQ83nIjlR0b+1tmqCiAq41ewaou/CDTlXUIbB"
        "HY9J6hkDA+Doivf6IBqPU7UwZfBnF6c3pCMGHEiYERMuGt/3EbgoRC1aeq5XuOXj8ZxSzyVJrtjQRx2ffRB8kVq1NaQrSQrl"
        "5OCjL+Je2XNVjd2EdA/b0evhqkS//DUVVPal5E/BPk5mEsl+a/B5MTQbGekElZ6Yphxi6BFLe1GxFHMUzGZvP/c6ZjDFyE/I"
        "cfbqp6dL2QFnluT7tXw970YXbv+tZaAAwnv7b09E1DRqNVCnPSVRmOke57rXVTZKPA1k9HsOGO7xT0OPw56DgPYMUeGzDdED"
        "mFNraQ2vBcIGBUkE3pj2CDoy5QDgeAsXG4IDvFiB0+GPOkolUtPCqMGywXP+kuvypvF5Ss9rP19VzW2DYRm4j1xyJ2lm7Kjp"
        "pOmznORgNDsDdPth/fYcumCrV3PD2XsOB9LIHa7ev/jXf3lDfbMd9F//tZm7m9s+rCdR5scclNPflfPeZcz4wIx32DY+Inea"
        "5rbL5F+Vb+nDwTKGV3lQru+Uz3P8cBu5d7yg2eNUiJ0yuooMeFWggKHgegAY4Uh1Ggp7PRqR6wXXo1guTxIK8v78wsTjNooY"
        "XPrL5j/0ymVDq0m5oTu1XEQq4n0/yWkt9NetIqxEDxHHJDoXz+OTb24OtePhqwjb1mPX0FhqGUpm2aIvRd4g0CSJktTicV4y"
        "R+U2axAPCk4Lmy/iflu3hTB2Y1oeh1fqr7b25MmTpn+FavUAF2RtN3aYdMzGBEt8o3kHgeBqkSw6I9C+3DqewxoK6kWFfPhK"
        "WOtOhQ/DfKxHI2UUfWwITiy0BXPcCny7lqzXgtsIupMPgPatllWOs79FdyNn0XUqrRtieU3Bogg2zrIiaGlJrjcH1RWRIwQF"
        "JNvvg2LOmeXtBNfKCTlRAK6flhzOnUgt95JKTsw5bMVwqTl0MTt/+7gdesQ9whmViHioNMAoeVP+kvUjyhh86NoH6jft5Svq"
        "rH2LfOqM1uQDkJtJp7x3YNR8L/8o8RaFixnXR3MQjauwfBcloUvbJNmXm3N3lOlRfFmwGbz+EwtQINbEDVD9RvG60Tkk53IU"
        "g7v+hzwfRbiEXjNn91OChGPGFeS8veH4RHfopf1Ht7xgJGRYmMYWyP1LMrFBvoRSLUdlsrmGmZD421t4Tlf3JliuhOGc9jy3"
        "U7/ZEqMvXpNeSganGZOaQXcu1xxKCPjGdr1kYLZLYHLuvQyTFl0+uGso3rb71FBTRFSsD7M2iLdwVnKjKsauC9BZ0iO6qks4"
        "HkPmnXbF4bcUfO/NjsH3Ro8xocuOQ+Sczgv+XihwcVERKt2UE1RcbiaXFTcG4S1RrohL+vjVnj5JjHbFVdiYay4/mekK5eWl"
        "mtKCcwCa5NxC57ZL5BC4JGQK4iRQ77mZ7aOrEgYQpCBHXzG+jNPnKfzr3JgHgGXkgR5RV5VJwvm5nNlnSdECW8/EP5H5rD6S"
        "oOWDjK6cjD7EReN+U/FRyJhOVPNpatW4rulo9NGleWZk1+SQWsvuQ3Yya1uu6oqpJXTBR3A6H10bPtCw80a5fmrOQMP5ZjXs"
        "9tuK6zqiGiNRZhfSQf7b7JQAJQ8df3kuzsC9smrgMnp1T+2S76Y0Rlm8s+BALlATyWkiEuPbPDjMwJKTqMA2lHM/gDdN/Gqh"
        "Qs8rjmtXDIvjWE7qaTn+ZFDczjBSf2b59ZC/KOQXN6bnpJguv+mEeTnn43M4A2eGU/f9fsfX+azyFmUe9SqXZ5SuYJx4AUQ5"
        "O1ueBkGabQSlxusrC7yLWTZ04ZT2bBTMQcykcrJMFZdQeP5AdsEQlDOWrSIP1b0lzMDtRTFJcYT39hYZr4UGnOV0UYtHeRYW"
        "sWK5BndapsHKkupdt7rJ5Hl36g84Eh1mnsLLBoVZLWaHt83yCneFpe6nM3IwRaTBwpm77gRrH0+uHWNfBvLGnKgmCZ8juTBC"
        "PWstwP/T8o2nknmlJxe5e1jYuNDj0sjQzVJiB+mWv2b5eiMoX3pqrmKIF1Bgr9e52/oNaE7jGorrclShCa5I72XuY/vkBaY4"
        "XOZJr3eaI2KuyKeKAQp56tweIqB31hRbnPViarUl+TL1JfoUUP1ww1ivl4/1VI+hqiHjygiKOyxncYGMjlNui5HIXnKiVHxQ"
        "ewYbb1S+e6dinJc5eVg26subRn1u5GC2uoAeecrE5RAZI5lRqS32tV7Pe7khLweVN0OzAF1CbFm6FJ7jhPTucm2xI74R8Wi9"
        "+kL6kvOVu0tUev20iFRs8rKeEzDDmPKbazHBbTqDwXDhSrlCACqEI/6Tc4QIKENW1Ucmi2bZaFxWsRUGGnp/LJpnXnlKNHVN"
        "K6JfNCJGokgFFghAqo8xxjjrXEyMJ4cAN3LV/z+22nB36JgjDLFqMa0qoFJidqxXy1SUej+qngNJ8KcwIC456tiSRz2Z2flI"
        "Op+fdYP0GA8qwakcpDqgp7fVDyMnUr/ajPTLgsYtAygOKM4i9OcTTRL/Yq5XZ8a5p7d358hD5B/xnbA8Yo1mSgIWQQBvfXeL"
        "ylBka8CqtGotDQcez/zbDvxQY8fzsCKqdowjW1jb614eViod7mj2qtR7qAKiKrjUiyzNlK7fGle6NJTUVUJTxZxaFXMnjyr9"
        "+3/4T7cFlhamPpm/o7stSTskAnnWUz+eI7fELYTaOvP4JCGGdwz/M8l0ygGPEllqgh65hRuVzDcr36OfG6P0quM5rm/3jr8h"
        "5q4ifuJ2b/clkXaYcmWEXZHJhkunEWkVYTcVMQLLaWcpYm8GkJ5t3OeCiY9QNMWFXpEAgZwJpPtGwSjJ57T1dDDwrnWWA+b6"
        "0qkCRJrsVFx9l5OLjNmBlEzhFlHeTsrIzkXxvtIwbm//K/CIXkvioIKG+ohWWJgxhVxVbPCRbCIRTOsG1GHNKpVcP5pQszMe"
        "7GGVO2KYLVU15eD5zgbFFQbUfrBwyZaMZO0SrkVHN739mcluHvQfPHCsYRWoU3Ff7UeNWe/Zuxd4r8Q3sLWua/f13Foipgj2"
        "bywRJePfS4YqCsZyteMYtBuLq3yINQuia8ljpr+bwbFcakjTkWV9H2RN3N6S/dP4n8YlS+Brh2352//KKxEYLynXieKC3EO0"
        "Iey8sBl93G06xdj++i/HzATrWvC4Qgv7naNXl7YoovJti7L0i+44Ye2fxscu4adqdTyTG0OlS2er7PIXYARoq4vekSfHOCZV"
        "9XFbuEt9Md5l0ej53GUdNZ4mWdhEabOgOStLPKLwCgCULnKosurKCRo48PgPuQdS7xyOK3imXxa54rBZ5wyO2qu2yC1lO8SI"
        "t1ZKrHlDcg/ft7Bzj2uW3EYWg+0kvt/LmVHwZc0X8TCexQx1y2ze95Jbp1dwoT9f2OMy5UThZnS6QClvRqzFbdC308jckTfH"
        "LMfifeZATz/2HQcks1cORVkVEAGAZX5CZkuk10SWyb3ZQCVW/La9GWxlo7Ijx5iHVsvMZ5WnIAWRMzoHhkV28kLE2dvzfAmr"
        "xBejX7axBI74asIWcgOfBC/kVj4TwpAb+4oYBFf68ZGLjUzQEvLj1I1/cNI4m1S+T57U646HqWNaOel0tk+LCAcinbjkM8lA"
        "OYaWAMzQC/abz3oPL3+casZF4Krivfkts+Vlyh3/auXSdORu7o5/UzcblbgVeGnShGIivHTWEqLGdR3rahdr7Tcsm2y+l3ay"
        "uJpUk6WF6sBV50p8d3As98vW1qNJss5NRqECRL6s8GptKWDgSC4js0Al4xJJV1+bH3xN7YB5yUe8IjbWN3wqhl8zigv9Zn74"
        "6+DEJeIDg+P0JZ8a+aLiRW+YMq7mDz+8kqipsGjE1msU/eFEHO5/d3D2/fGb19SNALc//aqf9pjcEve+j4bPVp7yHxx25MSu"
        "oQ++QGP4ZxRTqwiCC3jZq81ng7Udfp0ls2H8zKMnT9f15cpTnNJLANoQTUFqQDRpf682iK5qwQWtX7V1QFha88pNKCGMxxDE"
        "bSFeR5/hPnr4qsB78DwFLYuxc1kTaSL9uricZ4gtiuM71V3vZVn794NolAw/7b0CdzDtfERazH+H64Z3t/E/uPrsPsb/cMvQ"
        "70yp9/NucvkQImL68N04tm+PkPsyfvgdEtTG2aW24dT/HVhtbskeL6DheGWUz1Y60xQU8zOC6uDKsfFocr2LRyZMNz++rHyD"
        "r930eo13WCDrM555yc0aXvErtwlq97T/CcUQAwwNL5jhXQqQaxcxE3x2mJ3xAcuaUlwFvNyaXK9vNLe24SQyDdfW+BZqEQnQ"
        "7th353jD29nPBdPY112+/rLS7J4PrtEer9cQ+gEN6XXc3w14re9MRvHzWsLYeijhN3YDMfli5Li+DdvQIbsUVzc/uEYHVC4h"
        "HuVjB+Ey/X48Zpcr69844Ye4UWgiSYOPjn948eodLE3gS3AsrshmYlHE8A8t2FUCnzS33jfrshZNynNr2UwY2M84lroK8HYT"
        "OOCJOYL/Wxz88IqG3U/w0xytzZnwKxpna5B+kgF360IrYZMRkzO5XlK7qCIdydHQioAsRPjcoaKtNsTirTVb7XjEV71ZdNOo"
        "S/12AUFft6JWtNG2LwbYH2RITaLhGhOsJtid8EmLA9qWv9EsaD3ADrYeQDY570bhRnursb3TaG8+bjQ3dzQKZIwkkWQJ4ff2"
        "wPOML7e847e8gWaDouUn7cYmWt7YaDTb7Xu2rGN+ZFveRsOIkbJNP37SeNxqtNtP0PJ2Rcu6GgMuD3QkTwZdru1oDqsS3kSb"
        "Twa9lmw2sCBe9La624M+XwB3SaX+o/7GoKWt4EIKFJJu29vbDfu/Zmtru667SBm76rt8BjtQ+XXbDBLf1y4qSzyR+t3ZuPLr"
        "Y/u1uvZG27TfxeA3sIhQHCf9G7rpt28pt2MHfBH1eZBbuBZFNh9/pHirwf9rbuqydIf0Z+PfkNjJVBaJBOD99U53uzd4tJu/"
        "EliWPKvTAgg2Nrf7MawQXz/ubUbUF37da7Uf9Telg3ScNzYYmAORjovR7WBc7S07OBfMt+zaywGs6vhJS/tFww0LHwQsvmpF"
        "3UGvbqAl5Xnrw0JuliVsBcD0wcZO3q9zCNAvOv6yBO+93v/2iG7NiJBvCF3gTSm4GBPBWCMYpsFCz8wlKiPRZy5DgbgpNTMI"
        "kFQI27NbiQ0P511otIMjnJwbsZTBiFs5RhSKGZBiBu/E04DpNnFH/c2IcStHjHer7+LHlo8a7zEAwZAvN1+2Xj7eXYofv+7v"
        "9HZiXGjWxPx8ZAKaCtgFNLV4I+GO2VoHvbQOWlsbOy562fp2a2t7x0Ev37Y22o8OXPTS3tpobb500cvXL799ufXyZYFPnEEL"
        "BhEYL9DF1y83DnZe7ucIwn42+OBrNNZ6/LzAAAhCz8+2M2Y59e2qL/YcEZr5v5ZXTc62kHz/VB8cbO5vf3vLqbaHS8fIh8f7"
        "21sbwdY2z5c2UT7hpu/SId/EwDa9wd18rDd2bNfSY95bsC1d60Z5R1vn+KV8vJpYZ8vJLR1LUFmtA0BFfmhl89xvvIywi2TU"
        "qS2CDgQUmRy9o49DynqbBNJNolRvAMqBLcMub98dHx+wELNdCw/loBIoJZmZHelfJCX2OeRT3rwFQyJumFuGZ7Qpj9MSrjkw"
        "XLOcy1t5oN8Su7R/GXapYL7ui2FavQL6qnHM40fEfZs7Lj/T2rH8THvrcWPj0XYD/9/cJkOjb0E4QLOebAhRDTa3c87q8aPG"
        "xk4D7BYI/U5dKZOLth5v+9zV0oMAnurxxkvh2b7e6Le6m4/MWWjvt77d2hdpw+WmBt3YYBqL7npPupv9gYPu9Gy56G7Q7vcs"
        "hXa5qVZjh7Sx+bj9mzNSznputG/kox4t8lHFsm/s3MBFOX20d+7GRG0s8lBbwuaWeCiCAGnOVv6htOVlDqu1yGH9Kly82Spw"
        "cfDIw44GB+f9KAjtlrivlsN7OQt6K+dVoOjyiLZvHlE1M9YSybvykNV9vK7oTMTgTicaAG8JbqNhVOZZq+06wjEwdgr1CBAu"
        "MBC+rm1TKp+qiK4/unLxhjAvVnbfaj/gYAspeQ2eI+fx4iqUlw3wRbgq8SbY8+qw5SW7fOdWS4OEsgJjfMT93GpTj+GQJRiW"
        "QGGEXoXbFAjqMHlRJfnH8FEbvdad0mt6+0fHLI3s28e4e5nM1hAjeblsMZzRYdN5PzTR227wC+qsLGgdECkBmkt3E5/ONZGQ"
        "CATYKHqgDRnGmDhC8c7Ha2o7CAj/8XQ3UAXPFhdmgkBY0ets5NKHKH6g1V37mPRx0yqoHGnAbqHekVADEu6vCcLoM4cWcpqB"
        "qaaFcN0pdVpGt6Igb9Q3dIYKmgz5z7VCWiqnknUC64xKG3vpmFsCtq0q/Y05qHV3sxzQYIJ/8CZwk3Qre6+NEsrZkV3ZrkKj"
        "BRzuzgExHpiCp7sSYoPxcRoGHDeEpV2cEsgFqTma4yueYtk7VJCXPOvZ4sbKHvpbKL+Mfo5gNceWI6BM3i4sUyaQrqVz1Ve/"
        "XXf6xHDIMyoIZfFwUEBQ3mfL8Nqy8+5kN/iickny9pV3lB6GdK2F2m7X5ELsWHWd05WIqe3lc5TeP5otos7UX/y7jqcpQ1pc"
        "sJwmldWUlrJoU5Pk7jN6zBlt/poZCb6vmNN99huexrNYwDEmu/0RmUXtTH7lWoAt77uQy9+78ncNCGlC2WAN1ecj3OkZbAym"
        "/J+BbG0Oj3UXR21ZHNXeujOSapKBq5wFP1SsSX1hO/TL1BuKVZr4Io1Za/mVEyUh8D4iJA9ElsjBT8tLlU4+/671k6lmnKCn"
        "IVfQYnVdBajfzcK0ZA3wg1hWll7LoygUBGuCYy7arvaenJCFPjuSiyXoGLwqcRfx5ppDY+cT3jkaZXEJPMhwy7LcnUq11AyB"
        "ITbHLiwlY0HJClLi+FSubxZAQcWi7nYVkiT34zEQ94HzBSRTXiPZAUjMyM/5+Y4oyTv4NOFUEI1NQzQohGT3O2XNx4Py+koz"
        "laeEH5ZgjtIy9qJhL7RnBe5LokR1jkwrR+AL8Cm9Ny9LCyQ05AbYWgKNy7C8dnLld7LZWsC0O7LgPsHf2HabQFDd+Lw02O1q"
        "jF2JXReNSzLL4jXStsKbI8kKhKZktu3uVjObd+8MUr92HGRP4vNFPkSwEVvrBNqmgJUxHjoqotuIomF58H8tDz52tnIgECxR"
        "wYOIktNojW8ByUeqhLoHdRTIMzJUUQ+gtp01CkEBv7PyhLvuYnWHae/yNoq+ubhq2Qirtqyhu7BajpLugvtaecZFE1Eu/2uI"
        "/yJNtKJ2VS92kqa1ZHzBQGuAp97niIO9o7U+8pLJ2yDJAM62h/c3278Nc1wI1DD2RxQidxf2phhp4tmnCxEcopph11UY33Lk"
        "pnbbo1ftJfSqYtR2112QbbYzZ0B32VKvMCegQ2w/ql4qn+NTLfON23PjUSYsG9eDOxGXhfNUScYxqt/mdFMlt/wUiUouX4em"
        "hGlN4YEjv6Cq/Tc7TUX7sh7qEZJzuaoj9jmVti85trduFkzZwfkFL3X4XCWPLVAgrXJi/Lv7p6hWnOWt7WXOGKanj+kU0ajn"
        "3tkpTtsizcqb3gi+SuA7DHskRXa/tVxFdruCrHAgcbdriYrK09ss6k+3fFs/diEaAyy0y+yCnAXjkBDcl8xkBf4dwkfNbdEZ"
        "UR1TBC6xq/wxXFNtd/AFGGVpobwMFuQCy+7w+mtbwlVsVW7iUmGZrk63HHLtYK3AcwutL1LCCknBdpcfOkEefYZvmCXE1uA2"
        "zsRAD8ueKFicukQzB6/eBdiZezExridTe8cAN5txj8KW5UDuTGFu4KrvvBEyijIHuLVQY2Cwuqjqfppns2TwaS0/BBYFE+2L"
        "R+W9dIiLzLNIK2isL35lVjpvuTStcqUWaNrX7XZvezv2sV9LjQxWQb651dh48rjxZMuYGJSJtLstqnJ6gwZmNMuwb/3GTlz9"
        "s39+J3O5o3LpAdbvn8V25mLAzW09jnAzhOrpvOQ6B6VGIa3KUhUoT5UaHi+PRtYUaI1nnagZ4Vu3vR3MkzXYS1Mp2Qieg2rA"
        "jArKlr9cUCCJBay++5vKgY9KciCtngqngBPODbB2syNfSbatFPfdEbuWKZrLckfAJ26fN2IKOy6rPnJ0TWI1kK2W29mD0Pkm"
        "VtM6ylvl13I5XGGAHpTNeNSNqUdOcXms8+Lr7h/gYVseXonIuQ2YPs1SK/rdMEjLWBFMsEh/VTD1QJ3b4Yg6HzORHsJiaF8Y"
        "SUZIE7Rjw3ZpYzAduIq3jZ1chSAGiOpvcLS+v3FiAS2bhqyyIleTO/Jiu+12WS2qP7YDvj85sLXup+Nt34GhvbdouhK48360"
        "VE6+gcd15tO9Rcjc9Gb/q8VSaeZuEqkU/bcURvMO7iqH4nv2C/XaOd5jG5t3bgR31uA+93CzwabqVW0hQw5ucYewoQ3LryWQ"
        "vUx3/KhSNTcdny+xPgliqTzDVQeX7STjCbMrNBEfbZ/ln+bsepYbAZndUjT9t1OvO/mLywHKVUO/hnLliibmnSBOtPTC4rtN"
        "i3s42SaWEr6Q8s/90EXrV6CL5bJu1Wnm2O4m0XLL7oHALaec73TBDO5sOd+MjfHOPG8GNxUZ7N1JiZVITVUZzQljTfYk+O/U"
        "JSQGnXi9u/oN0wa2X6fk+N5v3w1infP1uF1G2eKOoezHZHZ/RnwBrJCkb0rLLY67y9qg7ebsdoyv5uqiwp2QvlZaun0QMe5N"
        "cMHhXi1RBuQixWPnzPyCI76U3a0KA9EB9RAZFmVLFHwF65pLQJRv/JAYu11IgZuBHiVTo9qftJe0Wq01cbCmrP5doNCTXB9X"
        "oJtHRp2+hKl+vF3QW/XkrbQvLLLVuCoAAb53hYCNe2omtPGkwk5oQDVnTD15dKNiATYLa/Ka8ZISyAY8zJBWZWiP7NpGofBy"
        "lXBGz9ZacLJZIjjs5IIDCXhOx28XIGjgHk3Tj9k9qX2uy5Dad+ZE2iK9NdtiR3wkf+VZMZCDcSuRVYUxsFJj+2uFzWJeUGY7"
        "eohH3r5X6Zm3/WWpVK8Un5ehxFt9gTZy5MrraW/078nL3M2QZuUgb6rtqqnKub+DaL0S3KK+E4PpPV2M3Cn9SjcTuxdC53Ki"
        "nHPvd+YR7wJuO955pgrc9XmzoY6lIaUS4H6TD0qlws40Iq4DTU3EeQ+W4FE16iyDCykO8Ft2X1FkyxdFch0A83p+9pU9/xZ0"
        "+ZdgOsNH5Me/0BRI7pQ70iTbGramveTYmiNSrl65V5XE4ElBDCx2vhEdWxHzS5Bv5m3SKEnH03UTBvx03YRXS4xuD9xCtldz"
        "wlNrSG02i9b4hgHO8gqlKWKawvRCrj17uo5XJlib+Vv0G2/KfAYk8jQZnUvwtQRc8yJuG3zNS65xgZkUchuleyVadV7JDVHl"
        "QG926haCP2btmZcagnpTam17SDPzf9i8Iz1TUwftd62umDXno8G+5juc4zSQXB7E53QPd17FY0lwkt950I+uIqrSIpsIr/bs"
        "//mf/tv/JjjCxgDkzOen69p4eRDilogxUEh6tq9xMdgz/rLDqdiXI/k3b7OiJMN3as9e4+9NpcS/vPbsLf8pypkdXtctZkR+"
        "lOSrQqDTXTT3IdkPokSlE5p8xfeLtk4r39fasw0ztSC/OswmfUJvbVPP3SLkbnhmvMm93QeG8IHmsvbs2L+u2ElNuAg+VwF1"
        "hrq9s+Fbgbi//4f/brEg/G9sqVdIAuaWsmAjg/NBB4yi1uq+N6kdnv39v/7XYN/cYuICA+oWDT0VQvDMTykUMkcJcyVBlE15"
        "l+J58rd/tjkV60/Xtc7C2qnmU4chWV3cEbsFDW+Zl5QfywrTuojDOWXY++xv/9xBqpSI0VqA3CxjqkkeD5NwFVlUhkUW1jDO"
        "mkFiL7Aegu1mXq+//V+2KnO90J7OCFMYYOpNs7Rv7P0hvHtILx+jfz/4A8k5O4QOG2vi5xmazP/2n/PERryKxF5c0iwO+7qB"
        "318Ky20Ly3+YM/OnyUA9tdtWDdCCuqtBmvlXnikA2HdgKmrPSqCljhsGJk2O/2dPE+wX/2ePsDtWaePZIUuiAL6RoX32Zx31"
        "FPkyoK+ghSDSJeJZ1CIWD7kQVh42FDzsXtQ/givzWwMC0b+wQBdYFjaHeLJXw8UANepH+NS2aC9jajC5I4CfYHrLO148Z796"
        "xWxy/7usWX7fjbNuTEkXE+zye23kBvYCj1Wu3W88Cbke4S4zOJArEha3fWgu+ePVSsOfo/8yo5brMO4y6mP/3jJn+Jq8G4sN"
        "n0WLWPTgjdPbJ7EMAVIM9w6lmdqzH7lGSPka6cWIU7kAHgAexLx6h0ub499lqFcTJC2DZdvRC3Mrm4C/l3G2qv3yocsvHFhy"
        "5rbzM7ddnDkQsmBMN5fYpNgSZDyNrq7AQo0jLvtNp7A8cr1P7v5Dt/chVA7dIosNf9i8pCvP+a+WTdCaZeO9YdcrN/2Q+bad"
        "RL834j9IlQXUc0LT/eFQgXg+M8nMDF8iYFxmxqTKUaxVjpitec7005GwgQXYR5j3fwrkbwHmPr9WNbqp5XGm36bXNaPfZGvO"
        "DsgFpaoU4K0ySJpda210Wi39/4UjTRA1bPHFmdY1rPEhvJ+AyJm93GSGIl7PmeK//w//0ed7zG0k7liQRGpxJNt3GQlr3n8c"
        "t8D278bdbLJbBcbCBgUiWhGGxbMDI3zPy+woG2QZclriCWCMR/eugSueFclLmI77kdxAegXcEnOMCA2xHMoS+F1gMwPjgmjY"
        "zX2TMuyZ3ND3889MuQgpBEQsv86uxHwuTEpgYvAxh5a//4//HNxw+2RxDu9z7eQvZMNc0IZsDpBA0MjCLuznNwfm+QOFG/Mo"
        "jZH+bG0xDxHNKBKxwODWWLLnPSYsQeJjumsEw7/9b0sAr9Tw4kaK86XZxiOTRs2C9A/mTgrLWbsch821LqBm71OkDPof/3Pw"
        "HqiHpCuqQhbualJvYs4Snogsnj011g+RCq64MvrCp3SwZBjynkwqqJyHKqmC0cL6+Oy3Y8Y3LTMuNzdW894+wr8X7+JmMb4L"
        "A3OE0wI5pZydMtBYY5xM4vYCYytCt6kur/QSVz+7f1Vef/IhmAYTkM3KxKZ+MxN/v+lLAvq7zPt1dJUWNxOgWqKw6hPB8sQl"
        "z4esC8FX7hRJR0jQedMUlsCYwQqLpwtmG4siJX1i+dzjuPwvgXftZy7H+TRrqaT/A3Mw5mqi+yXC1ZNMJcH/HXiZi287t0tw"
        "0jHTIytChqQi+Vk7+f0P/oaYFY8zXuU6UnZQVv83wNEmf6cPKfDTzNGsfhCMYIrq3axN7/uCvu4emJTedTUKcph6UNIpViwt"
        "NXa5Zs7wTnxAIXwqLcbTdSrGjJKUJZHHuZafKXUuzMmor3GZ9oV2+VpOpreG5DDPUiVXk2fvkwiEDLvYLwAIrJL8/gCnd1mS"
        "4O///f8ZHPVwUQzs7nz+Aeox+DJhESc+Rn6aSalnKzXKMNBeJsges7sCAgK7wz/wCjPcDwW3orQn7oFMVn4wjPn47adXuIWR"
        "gZ1aGFSFdxmx9BGaGZ/j7tsm+AzybuH6ye+ePqudrvOqFpYIP9d+hxyitd9B87PLvDVP5ddwJj+eyY9z+bFaW+WPP89T/PyC"
        "rOlMiIGYBeRf5MVQcFakNxHU67yNcsAEyHpr1Gu5pFq/O9c36CeYHfjTDn2Syi1TIfwwkUsYwxvEQHT8+RmZRy9SmCFqh++O"
        "jmuaDakT/Pujd2+bmcwSbtVhv/6lDp0964RS//OXfFkGGAHblvfvurzmo0mO53wcfqbGtVOsrXguUxULE58EpyOl2li8Of8h"
        "FI0zOiG0MIF0k67cADWk8UZy0/oX3t48HModIuh6MB/riZzMkYsYria4mZQxPc7kX8BA0ITFPtTwJiS/lpy0TMjbKI2TU2Be"
        "W2kn+MtfOD2xVeXd9EZyHRWQTQMqPmRcr0tSEqQ6DrFNv/sdNktdsvH4lSmgOYBpZlwyLHxxBoYuMLDP+KcTaFcd7U9cLnSI"
        "X6RS0avt6nPeP7KI8xYWhg88T0cjZmW2v0OZkjMpNPyady8MdTL+NslGUEnM5PtiM+FF58PdhYLufvI6U7Wr4uZ4BY8uMr0X"
        "NSCyTj8difkwpdQY1rwkBUjiHnQdGMCdpefMto0vgMxSV0hIPJRMIvmEkP2WqXBDLsjS/3CbtOT3sNaSwAjvoBasbIl3buuI"
        "zYtuNJUL0nRayYiHL/4YvGLeE93MZASL7zCN+vlx+Lxi7vRADSKPfB16uOBuFhtEE9aUy2Redp7pHpHQc0Yy4NKzGq7BltbR"
        "TlPsaFJCrX14RIaAXacXYoE30eyCLnDItbiDYHgMS+o1gvy31jbNnjcx/Y86j2SEO19YbC2vhvsSMl4s3na/mO6LT27pRqmE"
        "doMjzuzE9eaFXL+HOcxSHIboh/evw5pkj1mfQHSX0l/MctLXGeCn1jRvq30Qdw6jRB58RdhQHFnX0CgsDZMyYxAkbTgJr5tc"
        "3ucaL8JrKKUm34NgANMcpxPBoA6utUV4/wUaMuS7vtiQnMTfB+G3Pxz96cT8PiVeqQmzmPLCm3rAtN0mOGW3+vh5Z0DyfzeC"
        "r74yDdZLB+0ESCNBTBqOm8FuaGya4C6Nb4/f1usGFv+BFK1pI9gw3Lw94pOerJx5setUWByOiT/DiHLUwwvSdPuwUxEtNUGB"
        "p1PiLxkE0ut7B0Nu+f0YISJAqZLB08xdJAgxwpVwoE/jFFbbFBzjFwNQQh+14rSpCej1Q04Nd7lNZFAAdMKR8JoCZOvftXnq"
        "w6+UshJIchorJXLkyD2yKIc7aMyR9d0qmuXhK1NTKJysqxDv+q70pMhYU8V7yNp5Z38ZwGZacg50cU7SsAyZ9kB4cRoq/RHh"
        "IenHpmQxJ31Dcr78ji7t/ksgVD3gRfCfpaOHD3X5nuHO6uVdsa7THXeXE3Yp4e+Rg6AViAefYulfRRPSMXUOlwVmzTfIJwsk"
        "9mQJuLy7K8D1L3gKUpeTDMU+lxHRe4Eh9Y7VDdOHBPD3c8KwFFxdBn40krvkTA5HUILkilbwoWlqhStHagBkdoRwyt4FFKqw"
        "P4Zwm5KgKkAC32IhIzA0EtbCeeVrYB8MRXBACh4neYXdFbOjeHlAZ8nXom4CpNRG1Lmd88DEujwcUSxLw03RJ12hfOXcl1gu"
        "NJ+D9OJy3wzt/rIXHIZevSXH/R6oSk6mIh4zkHrg/czHJXyZYkJL+5WtIraRLGOEgXsGCPEKyqFI9aKT8CRHtbNb1nc0O7Ii"
        "wWeH9OLqCHDimThAsdzIfoHLbToNM5DMRy2cx2upvIbvuDBRgrzNvREj3mPXIcMV8uJKXKz2+6DWqgUM7uUtStdY+5eMJgs3"
        "ChGk1qRo0aiV9yA+b2PtGnTXAmnG/Urkug9lUT87TBGj+oxTh9G4ZQmN71DqsiavuTpBZWBg3iA/E6u83FolFwsaEWkoNDbR"
        "bDhw70pw28DUXIjAVrA2kzDletU814yrvVXONsX9X5z6KhUrfAGZK0xxz6Zc7Ec9Cl4KTsRbXMmJVTH6lKJw2xa2H+yi1XLp"
        "F1Cs9xPV6lXcKsYNkXTYBxauRj28EFjXtEA+V0TsgFu7tPpv6BdwzkjgtIJl9RlZ51NdPP/8ARqa7IzxVpb5SsizkVuvVLrR"
        "FtF5OxeDLa/UtfvoY2fAmB52A6dd7IDbhwafu9AnVkGZyTDhVcZwHWwIYA1jH/yM0rAv0EdF0lSyCerd258RcEIXKfz9ckdo"
        "Y1V4To/PwZP+Xn8R9mYe7KluYlVsl3LaMmmlN5z3gYxwGVidIBbwdo0cilYVYhOFWJTRl6oEW7XwSVicSRZAqUT3LC0+lp+0"
        "5XpA7pR1bGDlKgt2MAeSpdsOD0ShP1qlrmzV6MpWra5s9VnpFiCjXaotOQwVBFpXrJoy5zyL3WanyeuF83W9DHavBWK7ADa5"
        "wqi7jPDoPZbmYLFbQyPT8XPCHk/icwOEVrawoN+D0ky4GgX+k2azWQH0zowF+k8FkLqs8bAA/qTu4Vz594dXN4qihPqyDhuk"
        "oR+tRWrgKIwqKnGInXUJcTbfSoyDnlZ+pr3V/TyNR1DXuSVoASkzyj7GKtqpHkK5j9IQyiO8dQiG2fTG4AATL5ELTtSYC8oH"
        "c+hpIeYM6hVcEm7YGQuTpKPWhgv5FDZVnrhBNZ/hXv4zaMj65NIO9XW4FXl8SOkc2ecWQOEI0hD0hTK8qrXwQP+rrCkxXEu2"
        "zi9bKlxaQvvVbJ+sVV0vWSTz0ZQ8HJRralIda+h+Ba+r3zziZJbh1Y2KFgA31PAE617atzqU3EYYrxomepbm6HxyodKzLWRV"
        "XV/hA0YxuQAT/WdBCrrSroZN0I2uPy7nbJHlRflZT+UlPA6SeNgvpiYfHcgSccZsn3ZUYAmzg7jcLgeH8LMsaqdye3zFZUNS"
        "nXTKi0+H5pFcQZp2/IU3X8iOmwFQjcA7s8wtWcDxr+Pz89RFG9QoQGeolnAU2BfnncLniRYXqWLtLqYC7UIo7l3Ma4xAUgLr"
        "wtu1UAT31J3nRbQ2BVJ8EUOD6DS+WJ0w2Hh/wLlPqDvI3HJfDMRYqhq2V7Uw5V2paSPv5vm71+/ekwM/qZl7TIgHvh5sP4lb"
        "XXnUZBvy2Iqj7fiJPMaDLfynj72tnSfmbdTdbO3I48ZWdyd6VDu1HR19kE6s7yGRTe5UJz/EOU2exOELT675FD/1PmtV7B/m"
        "ensIlrxA+PMX3k3cM08aEiHKfF6cVlgB+Gt/5ur104J4lfU0PhTaChKQQeUbq+jynYiKYwp1HO83zJpiOcN5GeOAI7mEckxk"
        "c1S/U8e1gFrRcFf5Il2NVVus9o9ljZbbE3U7a2J7fyT27cHUwN8uE2k1/5oJHNeMTXPNA1eovpuvlKOUoCb2EK9xR8N2q76b"
        "4xmuuK//L044T+Rneg53DHMQqh8xXotTW/Fafdzquocd+Sub2OEfs4kd/Udzg+deop3gIWlN7jRqzr1cTy1+YVogd3Arvhu/"
        "MfPdepGZ71/KsswlyePRBzBAJ5enctF9eFl3hMzUw+qY5ns4Qds1VmiJ5GbP8kpEc0XUzv6KldXQGOtOXff4cVYWxqmnHHhh"
        "GlzVCoav7ilf3TNioMvMZh9zVtYJoJHSBGsoAkwlw84aVrym7k0Fry2AGhKUfW77x7zjgs9mVa+hfDAM9MrZ/tw5fFV6OcGV"
        "JlCKI68B4om2lWG8XJR6L3WulzJskT9CwhFvTsapgP4NtOxSJBDTaTpedSSRZ3nlKnlA3/vjN4FbZtBvPG/eKHcxz/0ZI6yl"
        "CfISENtbhewgPpQiPxTuwE/XtVjeVTR3pS2/DVds0kmjHTPnllw1y6+ctQ4Xmi9nznBa+J+DSqnJDqG8BNqKswquH8iqnNBV"
        "654pDpmrDN5KV0XitAVkoAYzE2/prlzEvUt/ePQNm1hnSlAt3yN3ueKB0VAi2TvHpxCSynakhyiRSx693fxOW1dwY5EKOcYN"
        "UFymWrVAuOcKOAz/tDiixJMHi53BPCJPKisI243mjG7S7DeaB7cDoTQ2137fpVkvzwG26dTvwU7BEtFeqU0y676A6Og+yEXy"
        "GRtskJ07ZdQvGTUshXENG0Ld1SoRLlo15EbUaqNG3dowCjoPhfrviWjlstZAU0bBVCeDcRX3XwqJ9kqNXHBec+VRreVgdvIz"
        "vSuwC1R4f1Dr4Pfuu+/FfAd3gQnZBbNi/Rg61vgQisfhe6q1eWI3hLBe5abJD9BlotIu3+VGyu/Ny7z/c+1rwc4JY2QGO3kc"
        "orR0Lq+E4r8nB9CSm6s/YLAOQe9xD6Wp0YQxyUekEaFnT8vt5l8dctBfHTJVa2Y4GHu+zsGgDIdSm8PLODz45lDj++lHwmdY"
        "M1HPOlJGodI8ybQjznVENX7jnPYZGMkCGhRZ23V6OZY5H4mqq+DK1XN1xEvjlzuz8mZ3rIHaXb/nPyr6qYOhT3Up1hVS0pd8"
        "yf5I6Vr4tAH0z4fNiGbc8LDZtb++4SLP0pnwpGVLI9TIYEd6osDXlVyyhADfnDQD2s0FgLg9x/yPl0Z6yyKb/MeQl9O3Mbtc"
        "nQ56+keC+lrAj/hno865I1/qlmMkQy8YsqKW0DwpUQG5QOHMLgILHzaFhStg4EaPAZWjGtTM9uTOrgxWoej+YFOgakoC12ql"
        "pI7/A/55+LCwVie+zeAa+8OjVRo12NKS1cHO6yShJh1mBjU27BrXMWeZcd/MBs35V2h24xF+NMyjEYgLaJmJaCIuzhTKvn6y"
        "FW12KSlJAFPHFbk0QqTjClkSTdQphLEFcIIy5dLAEoNsuAb+ms565GCpiWBSDjlfDrAMDCjgViZkbdimm4OZgbYfiX+S4fiF"
        "zmQEDhpO+nPYTUJe8yhkI8KKdRuEF4vYlh0rjy+lZjBX5hrMojcehGIaymiTsLpow3K6QSNxiWEt2cRkrOSmQtrNrk7rylRV"
        "jCCx7LGN3vayhGgQtoS0m1hxefYD1pkfxGeuDeDoOX5Y8NjJs8BnqB8ajjowKyFQqZepI4EAIOsKoIjNkEIPNDJE5+1ybJZT"
        "XZjdDKpq0synXTtLzS5gYG/1mQCjrC2U7QulFPhWn/Xc+LbKkgq4q8/iPI6sspgeAo6Lx8KdTqE6GggZB3XAjgGtYuITHmSf"
        "8mJlHMxrpelZ793AImmLEwY5UtBlPWzi4ntm4PBfD1wp78RBIgMieTTyTbBJq3y9EVR+BLqglP+otfB9kNgPA/w7SFSyuXZc"
        "Iq/rTSRJOBK7LxA4jJZ1u7sdY5nE7Bc1syZxAxXCvg27mmBbHwarCN7jUsOgDW8F8Kp/xMwyshj5YcSqmKMIA+NTLis0hAOg"
        "3YzGRVA6ZZvo0C+LN2vKZjfNQeKRl0ME3GM/MX+462yVd45d3sDUS5u8xmROAvqTa6eVmbgZ1ZjIpKYd+95EBANsqGtDCjE1"
        "jD47VwuoHj75JUTW2nZUi1czlv2bFh3c1VWhFP+cL8TiGjDfUe2GBkWQ8PfvkGMt7Z5o08/TGTV7OkFv8+pWuS4dLXV5gD4x"
        "EeUhuVt1XsiDSKosFNJrZgo0jE7NdFSpNlJ3GdeaqZ47Dh+dGU0IUtaICjk3++yWFP9i8FVtn0Hv1AYfGBRfwvhZU4qqI6pB"
        "/Zc56j95qM8mVWbvjk2y5F1atJJTZnKzkIFaplkirFzeZJoA3Rbm9NLdEAMdsWQuwQBfxINoPhSXqOXNeTKnUPfygE4cnRow"
        "T64/47PVlZ3mY86NHBii6ffO9iLfXuCYLKzhgLwd/QaPp8qDl/0WK2Fo2jMch0nOUHYszJoplUGZ3i3nnPSSc0otb0ayNyxt"
        "xlPNFtyJtcTLe4tzMk/RV1E60nwqxjkjg0MTxvN7RVrITpJRyyQ5HNTE4MUAWP0J/tL1P/DmVGS0qBUSH7S2mEnJSd2OkcjS"
        "+N6JAhOj/EuteCfccW4zSgpn0eOpdSGXbcOnXeO+oMqARnnNDI9nFNqyK2yr5k+u3nBHY115tV1VIjdKy9vwhlr39C7G95TB"
        "io6VD+U1XNFsfW5ZqXBOFTsMt0ayc1gpE5EiDLdKZS+Wy561wv/caCU4Y1VY1B19SC5uW2dVGZUYc3zvU4UUwY6OWZFnyPiO"
        "7TKdjwnhAJMFWV7StCA7NEI6ajU4ePXhTozVmk/Ceke5xxrjYWXY3lZ0xLuEAULUeN7ZLbDJbtjuGed3hq5mgDjshrzET7w/"
        "D2sb9WCf0Osa802YohHuTfqSIrmJjZMScKqjsXY9MCoBrQLU5d0rWptYPTO25s+lHBwBNcbO1p3PEXIT0W0xjw0Ngq26H/HV"
        "NI4j0F118q7S8Zkx3ZlJxte9GMkyD+QfZqLKi06Aqs0eMDDIboHZD1XizcdnsO+FIfNmN4InLbq+JX2w0mf8awIq9v7ww6uD"
        "4zra0nZW/l8VJQii"),
    "beatonic": (
        "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"),
    "plutonicedit": (
        "eNq1vd1uG1m2Jnivp4ikyyVGJUWRlGTLlKVqWZYz3S1LKkvOPDUqtRAkg1KUSAYrIijb6SPgoC/6atAXPTPdaGCAuRsM0Pfz"
        "gwbm4vSb1BPMI8z3rf0TO4JBSs70UaYlMmL/7/W/1l67VqutnI5mWTyJ+oeDKPPq4SdPPziYZb635k1Hs+to4k3DxHsd/BRN"
        "+pH3Pkzj0V3o1X+Ok9t0GvRDb8876yfRNEvx6UMWjaLsMz65TfsrZ5E3SwMvmPRvQm8QTrIkdvpqeKPA6wcoMIi8bJZlkXc9"
        "irw0S2ZjlI1sY82VlQ+TwMuCXjhClUGMkaTo/E5VeHu8fvLhHO1HGPMvv0TeIPDOD48P3x969Swah/14wN5ZlV9H0SRsrGTR"
        "NPZa7W6r1d143nixte3lX7udDb9bmIqXRP2YA4tSTH2GwRx/OPlp37bn9ePJCpYo9v42s8Ooj1bjJMJSBqPQS8I0C7xokgVZ"
        "FvgNVkA73jhIbrHOXOt+MI0yNNH0juxcV9IIXYfjaRR6AQpPYlUTU5mOgs83YTDg4untaWAOWRKkfXQ5GcRsvz/Lmn9N40mT"
        "exFMkxBDkPEPMWpMCJvjTYMkw9Ye3CTxOEQTh4NrKTaOBwG2NVhFxSn7HXmj+NruYN/HvpyFo+EaRpQFaG/Q9dBoyuYH0TSc"
        "YMd/CTx0EyaToOFdR+iOo49G3unn7IafJnwXO3NortQAoSvReBonGXYyC7nG5junYj6Pg+zGfI5T8wnrP7PFE/spvZkBRu23"
        "WW+axH2M1T75bD9mWO5hNLJVs5sEyxxNru0DDmglSz53Vzz8DLFs3k2WTZtpmNxhI3WxV0Ea/nh+fvo+xJjS7EdsyShMGt65"
        "aY8vz6TKSvipH04z763UPEySOOl6lT9PzMq99Daaz39V/063tnoa92/DrFjfjvNd9OntZEUK90dBmlbNoF4s7fbiq2XizyAI"
        "x/HkSq1o6u165wk2a+WJt/bNftDYQTwhGEbxhEij4erbdmI3P9GEERsTTaIQuA0s/9tM0A+4OwLpiwISpArCaXb9OBiHas+l"
        "Sb38mvTq4asqXpB6V4O7pND1rjxqplICmFqv6To1f2Vl//QUBWouLat98wV/s4CWrM9TkhoGWAOdmk5npEUjr5fEH1NSwCQc"
        "hCBKIIE+miQoogJXDlRsGvX7JK4aQF8fHp+/P2Ftxaoa3tnJ0Qmw2hNIj0FkxtMZKEsTLf1AlqJWz7ApEiIQHaGEniYEcT74"
        "4UyVB5X+2yyagDbZntDedBavgkZMQjX+dIbuov4o7pIoW6oKUo2hRxwJPkWNvAkP4wnT8HoWNr/tPlwdnfxwgs2O0+YUpLH5"
        "1zia1PElnNxFCTjAdZjVa4CH1/vn+9iCWg3zq70aBf3bcXAd9b3XYRpdT2qNlUrCUyuJAmzhbDYlqPLjmxnxTR4q4OZHvd4L"
        "m5xqqLwiW2lOJ9eA2Kvjk5/fHmMarU+t7Zb8eNGQk5oATbxdAPMkq3nhKA29lhBEoB24CBkLBoa/EDVGwXgaXl9HwcrV6dHX"
        "LonBFQ7m/MfDd4dXb94eHZZbQcMom92E47CZfcpqvibPGMYAgkkaC6d7FQbnbGvl6uzwfHE7GEJzFH8EGfW9772acGx0//Ph"
        "q3Jxw5w4dH4eRKjjDloa81eOTk7+zRkq12tpNhtEMWc3CnqyLZMYVUkczs73zzmgLzVsQK2L+eMtxJRslvLbKZYoi5t82Jul"
        "n00B4ESGz28CbAG+TkHzwwEetJqt+5WDd6//hBaFCzf/xN9137NLI7gAnB45qNJQ2BBl8sIgyQz4KcXu4lEWrJwdvv/p8L0M"
        "NU3u0NkxiDs7J/ipb/crKyugIBBRgsGV7Etdcx5LrPnzMcpuQH5CrHy+uWgpwQ6Cug67BVjN0OWwSW5V95sYbTSt+7ZAEmaz"
        "ZIIygM+M1GRusX0Fp+ax1NRE/1D+AGe65fZsaTWfNLgL9XyIAVVzAoSMg9sQsJAqeAo/RWl2Fd/ukr/6D879Y+Xch82PSZTp"
        "Xh8Y+hRigR7vlQw4BXgCMNL6NxqvRR+iRzYmD8GwUXMC6R4d7dZm2XBtu3IiIv8OZuNpHa00vKHvDiQJIUj3w8oe7LOvmT4Y"
        "3NVHcA2wNWgf2cjsGMRazSdDirsuhwS5iHoglUIygDojiJjUBLy6qBQ/q9b8JiVjR0LoZ5+nYZoLceq7eYsx5O9ngGP1usmx"
        "jUbNGXjpRkdVjmeTAQpcXCohj9Po9+o3WICRI7xNUGTW/CHM1HjOw0/ZUTi5zm5+rt/kSwpcmEDhG6Do21SV/InTG4UoVdyX"
        "Xj6oPlAsC69moGBQ1q56s+EQxHCCvWj7hTqlAfzMUfYaXkVJDKTXvAtGM2EashPdOT7Uf2gIzzb9uUoyhgNKwpTcZAj9hldV"
        "EmPoqzGAeEC4SAnR9Zra+6ufI2w+J1Pzu5UcUjamCYCCKuWusSYUIjyrAR1OZmMNJnU9H3DRNx+OD87/fHpYN7DQfHVyAnSz"
        "X3/8+fi18/XodP/9/ju/3u+BpbT8FacvGYoBcZD8IoinkOgghQ3JMHaFMeRAfw72DI4cGoIvqrgh82eH706hpr/e/2n/+Pyt"
        "V1eWAi0/90cQ+24iUlYtdEDjZKvncb8feIIbbrusOqFyDEExBv8A/1/16tDbo1V5nkBvSyFHBh4Vc1CN0N8x2INxcznZHrGH"
        "eup4NgEusvd+NAabmkAh1/WIwKtq5qtU82cj6IQc0TQJHDTNxZbvlNgyR+tb88RxHrkfg+APIrmCkzODPKcYWZBcqwaB+otA"
        "Qn01KHKFx6Vv1aJdCcsWV8+/QdLOLvP5joLrVEmBrVbb+0f1oaM/tFuuRqwFwHQKC00glqOG2u9ZL6DcPZzF/bhAxlp5P3Gi"
        "aAP3We1oiQuiwA1fLiTsBXkhnqL5XhyP6g6lOoon10Im1jot3/s9prDty8h/Prs6/Ier85PTdydn51XUI1a0tC4Q6mEokN3Y"
        "yQKCUdxg9ljYyPpam4jt/C/LPE+3YNMaSle6f+m+WzJEaMQQa5JIacRI4Mk0jlA3GcPq9S86yEe11fl1E8YSd/9FOwTD2vXa"
        "ZXIweZyE2DKEuJdA9LmS3VEgqejwxyDKdtuQx3MqvE8mEhRUVMBzkIRihFUkT8gXKWAwKpHpulKVld0mJ3AcQzKb1B1oDEWS"
        "oIWsyV+iz3A4uUx3A+2lUOAlK3XLvLvAZPTkfJfNiKg4v0lqiYpCPDtLR2E4rbeaHbUX1qjXVIarOtgzmPEuptPQZirVg2Lc"
        "kPpz8W70GQprfFsf5MtLwx0K/oLdihTriLqQRKeQgmOPCgBUNS5pcBeAPNVcFjFQWihbhPSKhVaqG6FwcKEeX5J/QBY1X/NZ"
        "Ow+x7La8fe8oD0ojYY+FSrtaVSlqKr4ZXY0q4LTGcQ1kTELb0JF6funroalJqIcNr7Q30qGuYMij04QtV9jz87fnR4dnjUJl"
        "uwui5C3VMSp0B9HyHqcy6G22PYgKwU7rQwAhV1WLOY9XDNDWldKt62M9YNG/L4zGzaUZO4Whk5dLUk03xZQp9kqbdevV1t4c"
        "QpUt1hrK2p3nTRCIZlvMVtOAXgqvvo7JUXrCgKB8rMMhsc7PIfTY3JYlwGvxH0O6gliVBtdhPYUzoOH9IfDLC2AKY0Ehyapi"
        "MO0Hmv3v0ioIYS/g4q2L8YPuhkG422m1nMZYsckmrmD7nMLeE9ZZyq8oQM8IVqUGWzBsddnaObqp6e4eU15pNqgBUlkfAYo4"
        "XN9fWjWAALrGBpJ4pOwsazBGJmGtVCuvlNZLrz6KbUfp3dJjvnaD+OqHw3NZPBe1FGBoI8xlkfbmu4Dn0r4YklKsNZD1jzX/"
        "opWTigL2aOyfCn1Yr82TWWlNbefp/g+HVz+evztqCmaB0mi8on0MOtr6TTYe7UA6DxKA9a5+uTLHfnVnFuiW92qVepgQpHjd"
        "98sDqOwElAzrePbYxln2Ynr5Fa3Xa+u0ZxIC1ofBHdTJSRO/qpQ8h0jReEsC1aukRxVjNGYpVIIUdh2uK/tpcUxpuHSePdoC"
        "lW5XM7sFc0hE/NtsOQqtpnD1V0l8G05O4dwTh0VD+Vr6xFuoaGE293S/B9EiHMjz0gIIaSh1YEko16A09jkALU2GmBr6i4DQ"
        "TGur1Sot0kLiXU3DgIenkNZ/HSJS+YC8KJWbmgYo1lkiPD6FfWekc5OnmGW5EiQkaTER8iFwMfGVKUbYeq/25b7mNwehuzSE"
        "NmX8Ur1JmZXHLQt7/3L/VfSlhOFhVlvOcgfOWErkoT8ePFRZLSp7kbl9uS9uOs3UTbiI6qYkmwSs6G+QBWsusZ9HJC2J/xpc"
        "KtSBEFYsbEQcETuvlMPLCDgwfdNnWuF9rdcsQ6/RdNOwgoFvajarna/LhGHWkhFcQQsO7+g8rhSNlYxHC31zNqXLvo6arN0Q"
        "zSJvJ7kKBgPw7vSifVkwL6naF8qef2mkvFkyqhOidmuGeBoTOd3d3fX1XIp5Olh/CiH2qVcvNoURoAErN5LWagmzjtbB3qNf"
        "5g20EIhmdFfC/ioRDCkYO0JIJuGq1wsSmHjEUFuIl9jBttLp15cQjdyvKS07vs2m9xZ1Ax2OYnUxE58izWNrCnEWUP8VG8k1"
        "L2MeuaitrQH0d2tQs2Q++D4JP66pKdbU93htCEt7tgbdxj7BeAJYrdb0MNcww/5t2VGHoqqhNS7T7tNB4+mAa8xv0hDtS2uU"
        "U9Zgy5dBFJxV8F/RR5PEIEvxle6q5iuagJUSKLy4LBvLdsvGMtpgepBvhbuW3XfwUl0nwfgNKF9KFF72vv5p+5mPQtVGq3LN"
        "o5OD/SPjHfT9OemIQ6owKcu8oN1fFNaChbEYP8Tx9Uicpwp8+Gk/F375tS8vmuGncOFIzXDnmn8X9ZM4jYfikyVcVrQ/TkO8"
        "UO2rtRf6igiYJkhQhsUeyxYMggS772xDPrPautNmuq4mpRGC9up1zc3S9XdB/+SsWKDSHVxq0c5DkKuqyWKJmpmIuyP5eFX0"
        "TxO2h/4NOSMBaqJktWsZ2lrf7oZ8imZjWSrTyRqXzCwXa/fF/MUOui6H64uObHZG3FowyPuPEWRsSFLzVCTCi/4l8EkQnVoI"
        "yPOuU+b14U/HH46OHmF+RdV4llXV5aswSX5ds+IuwV7JAHeV195fYI3JOc1XCl2OPfxj2HNJqm44f9qURQMNNLQerA5RGVAR"
        "w7td+oeLmrQ4sS9tBNKKNmVRjqgr8haBe+4qNzMKgxvWfcecdncXiW1ZsbWGCq0rUPSBjvLIFWe+BJ1dLZZblTYDRABmQVeM"
        "p4gbgttjxqBF5eVoeqsczaqKoIuvYWPqeKlxk9DU3V/FW20HR6UhDHvKtZF3DnhdlYnAYwGwhB0qmISRL9WDfh/iCH0mo1UG"
        "vAANY+WmHAV3cRJbvuN6boW6V3huZbt2SwKM5oOUfWtP024+f9giqpi5sHLQXiVBaFGswL5FOvA1B1evXXOoMh9BAG0sME9p"
        "wYijDCnOwpySXXGZK0R3ZbSkfFcAn+5i8RzyJIAiuUZjImuyfzZIVIQRcmtO0FYRE4fjaba4JQWN/F2gOeMBNxfdlJRaPVaJ"
        "3SCo12pOcQEFpR3gycP0ic1foORlHWNZrkEp3c24CcZBlLvoeiO67jK8Ha2G1Anh/A7oWaPEM+8A0fGWCXSUHuKUKrQ/ZSar"
        "Hb5/fwI/4mCGwC6gwOrTdLX7l4mWCGUFbSNNMrggu8Ko6/7FWhtBRt1L369sW9vraqK+YtCqwbBYeMgA31HFks2vf1GjcY3g"
        "aw787XmdZqvY3DLgdEDhIUPsUkOrlJUtU15Y17UaKPeBUKnwYWDBUDjYahsG39T9X8sOzGpgIcoqN9iyRmcXPeZXGQtc72zJ"
        "epVaEHQARJQDFcDrbhcxwic25tnxrJCYm9jFeFWRu5WSL6IN/8yiJl0OMQzENwkPAmMU76BKYENq5BCpDikq7IHRfxgnddmk"
        "5INdntQfZ6n+xoGhRzECC6lNYRp5KD6kmoQxRdCFeCaAJQIJexkZvejg6K33DnxmH6cEvnWU5Pv987cnx/tHV29OGSD3pbPR"
        "fPH8WdfrbDLUcN1rw73c8Dov8LTrbbSch/PQvPm8+WKr0/U2t926W6i72fWeLa/bbr9obm93YYl3yt0jUPD9+dV7RuUlYZNx"
        "rOA89aT2b/+S/qH+l8GXdqNz73f5yfl70Wheqpcb9z4K/k5CFw++upnuzlwzB/AXvD8TTfPP2BqlU75CQIuoMJ8D0Sh+SMJQ"
        "PrwPxfZxGk3ErHE6S6ZK2Xkz69+kUSBlIJMVVIDaEdwpOCuQSPTo7eea6DETUWGOaG2Q54EyqxzAlS/NHAAnx9ZUQH/z1RDm"
        "WvzTyDtk6B6MY5kYBvnchFjamK8anqqwTxPb51sBHxQviT9RwAn6EtxXAJwmDPTj1HV8gprAjVYfgsaoqnRsQpxpV9qNpNVC"
        "fI2eyJRG8qsUklPWr2cIK8IQXfFVPDkSY0rysvrjj91377pnZ43xeAy5DrDj69jqBg0SqYSUxTyzMlpFS41Cpe6bN6tWshsz"
        "jlzBHkS8DHoSly3r+9YVOM5nArf3mMEbaHBMHX7cvE7i2TSdD46kwHfje3/wNoAOwHx+R1U+eWa+p755kTZHf52lWX0Dm9KC"
        "yq2wAquYD1HAetEIVbDCNK0c6XD47UY6HHJsCY199QLsaatWEGH1f2Lgl4gOdURBaeJHcRsRYdEAAeNPE0Q8IRYXwcLuRnr5"
        "V2yRT5EDswSAEPKuDvYPfjxU5ldt1EKc1FQGACyHoDO6ypGANi3SWowoMzYmFocKACjPxDIyhsYRw94nMTQCMBmj43UsIjxY"
        "udmpwGhuw89c0GnEQJcPkwiF3w4e5DOqVjTgYO3G8SGla6KZneMS6Zo7aZddj+AA04Kth6z3c72GVlwPzXIBY1gICcpHcIFx"
        "UXAbcoxDiA1tJSGoJVaiEt3lnjITEvCobokiaE6GuUheatkGzCaIgIfyKlwRgHrrbF7FHvGs2Koqu+puIg/RmeNo9VsOG2OU"
        "zV6Xj+alDqIzR/pgcUQbPK3AhgfRUGIWGCQ3m1zf4CCbEj905S5BpFgl7KvzE9AoLKDoCY+DT3UwREElGkT6YTTS0wRa3YJW"
        "tsO1Z7S0q6XI4iv1FoZXErlU6F/R9svGFOLpMmipiILWhB720ys0CWrKsroZIViFRtAAqYxfsEjXnrY6g6791Xja2hDbJ4iU"
        "t74uRAI/Qv/wVfH6p/hrnrQLD56qHla+uYR1Rld9VwVJ0BBCIVYMDPawJI4jZgGPSdZ5UCbmscsJYJS/6V/+puPBMPRxAvg1"
        "uoVQ/IYJJemKokHnE4fbhf2XDpARLb/mhEE8HIp/iN8MWtZ4ACjhs1wOoTo3m5qzCPcrSpORUw+FA08NnER9fyAjc7sBDpqP"
        "RBMOBrslQoEZyG3e+OnR/p9/PNx/rQ4hhH/TpTELfFprs2IUjgamataXT/fFuKHcQaWjSBwFrRxGFOZ7WDgw4dWFfzAsoyHn"
        "UhN+FlRkKAcMlinD8iQkzyJkMW7J0F0EZk9oCoIspD1fsimMAoFZx5Fu+Di31RsZKZGwIAN4F11KspdzZnKnEzqQon5WFbaF"
        "Jkyk85fbroRdJDKkWxHQxEEJh2IXEvmldH6r7LfiX6sBrCXiIlA2XIA2rLWOz8/Fj13pzAp6uh0NgTn0FWW7uZW69SU2pDQV"
        "9nOrQrFuL41QedGFjH9ZivdS4CwBX0rGLoVLqfc6qkt/sUFZGvBl+Uv19Cs3zso8sttOyMurlQ9saIjtz5IE/ObKUH4j7ALA"
        "/ipqhTquC84LrvtXePvfBROEIVC+JjdWtfWreoG0sgWnzLntQHzEbF64rNi5NPaA4wJb69moKtTLUuz0H7XFJxsVZRJKRXzC"
        "eH0j5D8YZelWyW3KSTjE1G9KVmWg2REOnBU4v7XpkiCHNKXmPBysc0w5C5p8nRJhBrMYDiR6Y5hWnQjybMSQ7rmNMPsowcAj"
        "BwDeHxgHLIF5l4QIyyZ/hcLttpRCwQew2TohbGWL1z5stU7gfWFe+eCbtTkx2lon8zjrwmapgGs2DVMdYsZAOPV2ncNCd3sA"
        "pgzsuIsGYWyCIORsRxHPJHyWMpOq+hba2BEo1tuJNGLqY9sjaePi8lHmj/nBTkn1VB9nCkFAKfQ6iGD9PshMfNfixlUzek/L"
        "m+SCmdouB9rNvk3UvhlNVnYvt7i7G2ewyfvn//o0/ef/BiVjoIRAWZIGTJhsqalc5e8PLhTNBA9WXRSFoGxkEFAZ7Rwd1AWO"
        "gMAym6wybtjJsVDMoAAISmCEIJIYt8YjwbzuLIhY9EY6WLWmok85C/Jyh/U4iOrgrkmZICZsO5d3PEkppzugG8nvOrI2BBRb"
        "12NmxIhGXimNREXuhV+Ls+7eHWtDYomCAMt23LjmrG+B0iGhEtqj+eQ8JFPM3S1bF3JDyA4ZX7dmCeUyYC9Ae67jVk/p1KyU"
        "HMzhmdweXSn1pzzOphTbwtyUo4O/i0wXT7TsoPZeBx8ZoczGnYR/2zXPLkRKoxMVNAYsf5dCP9qxgg7mjLMGC3zsIszt2uJK"
        "tmOs3URWqb9b1i7mUdHOHTj393//H/lHgQ/BC3ipkJBGnnzIqp9LzGcaysne/BVHLLPxHZC+hiJGG4iF5lMJ5a+GUTrbZpMy"
        "MGsp8+sA2J5F6Zf55iIqanW0x0KY0cxzpbDKOOar39UwWYoidDZHBQsWYW8YFTTDnBoWz2cNXdnHyDkMf/aNDaNeC0XtgF4x"
        "y8pxn6byCWRdqaYKP55BjXgEpYz4JXVVfgl4DamM1jE1ZdjicuVPnoG5Kh1VHuKZ+fYUX1w4E/VhXuh576bF0ZllSplx5Jie"
        "SYlzoXXQywWwZ/SMJNcvXNGda39BFLSyNeEQjyj/22eXLqiyZhECxGF4rlWru1lMES0AYYK5A6aNEQ6QSeYcUZpjOfoScsh+"
        "mQzTbCO4UmA2TteqQEXnjxOu1EkqzCiJ7+Z4wG+QwseM1jQy+DsKnKdUFHSo+TLMVRP6Kv5QAcl2GeYsk3YRCszCnfqKco8p"
        "FQ2WbmrKgTq8l2qzeL3FIQsnheVS0n8oRq4OhZ4fuIYyZQpDgMQs4E6qsyZK/1NQhC9XuXmQNmZVloZm/dEifYsYH0DkcVF+"
        "znZpWnQIjXgpVGs5S26wtSaN4H9wBLMHWG9hfU/UKnExcTw4pKnA00azyK9YVbAluH3X9pRkDtoJ2JopWV0IsLtwmtldO1YB"
        "CapqKKQNpUNyZOJfoyC/J9RpwclpDRAJODFCqAgvSIPlqTDDyZLwZ3Zt7AX1fCkLHEKTCsUgzCoyRpE7sCTEaXFzQmYWtOcl"
        "y7aoIp7d7tN7JQ6Y8II6FtFlTXp3HFIbeyc/HL+VkAZmF6qLaO8dKmO0rxPuML+NycKVhTxPw3PQyIGlwzJIZGDoVUf/RqvK"
        "kE2/v8QSOSZfZIhLmxgMDU2wMUEmQ1DcJIJhGNVFogh0m2ZAaYiFCyTN2fHJsZwo6if02HjhuBcOBoi3IcKHjmKiEHHC+Oac"
        "yizSCFXZYGHZQJ1hy8veibsOYTTyV0C2YcD2Ie0UY5rXP1kuUmifD+ERWui84UsNLQflTIr7S0cU/OoRBTo9ybIRBUtGtJiq"
        "L1tizQ9VkQpGcDSn8gjRuglcACkxQL1usKjAFgelGSl/euCdn7reJwR9CY9S0elq4MHXFYdhHdAZKZYhYB0q7GowLCZxUVED"
        "e6jRcG1PBB6lbeu2pKZ+oXC0yfZt4J5uGeGtiPqGtqnwWWfKY4IpKdDUmnlvFo0GVwpflRFETc6Hg0obXEZXEggjzkB1Itdh"
        "3XwjrFei5+zzw8lAPzUU5yCeAplxNIFBh5bVDlQaBJPXsCLJoZM6QWU7VCNXZsgr2mZza4g5jmpslA504Cw/JRWTArBpP0wQ"
        "/Sce+6GE9dSe/vnp+Ong6umPT989PastYRq923wlXs9UuHFozY+wH1692j/4Nx9OrzQhzhfHmGdkUI/2K0p/5XA9PIxSwQe+"
        "KVUoLtFfJmoLBowJhUZGZ1xXgttEdezdugZKtWfW/PPu5BjBTfvIyDiD+NjIYVY2iJANwUgxesAd2DzSTq7un74VqEwNRWcg"
        "KUr9QuIyWRPJxCZFczdevNqS6AOcJxpyYcGXdvWENACo4H6139rcLix/bp2Zgufq4MO53kscaFCC9njaPJAsLRImaTcuT1DE"
        "9e64EZuqJtb7WJIBAhVuEZYBX5yT1uaB1gEWMFcMfMWaA3rObh0IuM0P8pPMFTqsIHXi4UmVEYQh1ElR+ldcuVLaL2l9aubS"
        "nd38/VTABOFUPZwHgOEEvltNvBhiaVk+uHgSyeF09n8tEjIkssgzGxo1Kw5Uy4rqLpdw5zYDXyZ3JXutqrU/GBRYo/+41h1+"
        "rloPHmg9cA6xP0KlZn0hmKI5fKwglDr7Jd0wBoKNg4Yyq3ZJKo3h1qoLwyAaOQJyHPfsK2xTKm21BM1EvD44eXe6fw6sPXG3"
        "C8rpneyksA8mZejLSQjoGQARk2QDHsIrw3kVawQSKb5ghXMEh9QDSZeU+EUhndJ0UUh37Vjv6LiVBIYgQDENysD+p4O//9P/"
        "LjgRNYQJsQ33zBxJnfcStrySnMI1scJFktufKVyzPA1sxehQzjeazApUNAAYGDbHLeiB+SlmV9K2sOaFTBsYpt1rIQqlyEtJ"
        "LoyjWr+A7iItEUIpT/KpO9uyklfByRQe7uKqnH84R0Y9I9kqieB7LZbr/McXQaPnU8tKkYYvXpMMuHDoChICLCKnYQbjp8j+"
        "G4oXX5oUAR15OiiXQHIgTWfEcxODoHivRAgm2FBojyaZ0+no6PDopJkDfkYw+UJYuF8pCpANLWxaeCrpLFBNqLtmjsTU0F8h"
        "QMxHJHOfUvzrVYT0ZoCDASIwKiuFyyu5bqtZj9NJ5aBxHQWcc+9kWBR/xNNVmCpC+z5zor/AYsQWGtLORbt7OX/Q6zPh8lPF"
        "Ua8yYPKHAQKfEdnyyVscMVyM2jl/++7w6O3xoSB3CSrileK5W4YIRiHsA2qrEFwiGQ3mx0aPJDM5SGgOoIFBPRec9CWPI+KZ"
        "CiowHkmmtyqEhNlA3NO5pusEQkmDCsdxwnTYmoApU06Eh7b9PG6ISiSirqEwdjZzD6f7g0AuC1vWLkUVpl4Rnd/KwTCeJf2w"
        "QK0r4tHZuJEDGCjemmfSFXGaBeEtZ/m75aA7sQhImNX8SIvGnpSxhPVPAJACEtGecFtxCiGaDGOJOBm7C4LQEum2ltpZ41EK"
        "I0Qt1GK8fEdXNrrsNeWWxVaPGinG7VtYCz6hKiGrdsXol9v7ytUk1C0I78eIL9RwJemG5Cth8UWHw1ihCOV3AuKKcy5CoZ/e"
        "vj48KZBDQPdnnveFdKMNERUKsqFvdxXEzYR6uPsimC+Co0PkvD2g+OLZ07xawNG2chq4ipvKX1ud1IpjQDPdhXul18dyUfbB"
        "Gv4cJRW1W5VeTL6Wn2vQ2iiEfglunFKj83j6ra7PmynxhHnXYr9qG+eU7l+63i9QuouFg0fs+f6H128r9rw86cJKz0+7xOgW"
        "AMK3AIaFANHxF5d/aPOdxTIAIHu/Ulqr44OT91D/rDlBm9i1iW4azXiYHXl7Gtr5Af6AvMtnh3/6cHj8P+ybo+89BO5HOk1i"
        "3rg+pai9+ZSYIIuoA+5WaArVQUUdE1A0VYbGVInDX6NotVFqnl4korHKKlfXQlDD+kvG9NjfBY6co5CKhupoFFRCIQAOSQea"
        "0xj50a94OkEcWQ8fZYKDVemGkkFtcB5b5eui0PLlI87EVYgQC8VhcVB+8itYQqXUoXEdo32kmGJFo7mzuZXLAPNvg8frUAF9"
        "FE1mDefRa6ifnO9XH/ByOsjVse+1ntTwXs/TciIWdvziTr5eNbw7Iex6UyDI0VkeLCJ+ik0pq7ZimBIZiDAJBYMiOTEzHs6X"
        "yk0CoXmUx0WLKQzhCXESLGjfcuNLEQL4yGHZly5/xtwVFbcwWrH3lXujZKcKAGVrlxXJDh/cjLkTdyZw0CpPC4UnV7/iaq7l"
        "21lU6owCzELfy85XFdVwraD1sZqkOZAnGhN1NrFzPahWxrdFVXFusXM7wzu5tqWuUvnoCculLiMCqzMWFcOqA1JVcE6D8Si1"
        "R6faybMPjVPGxog6TvOaHHL1mPhkoLzrDO5qmBtlnqZNbcUsWbpcc2Z86+jujULAvN4fkWkRwbp9aWfnGCmtuxtqdj5qjvN7"
        "MV+akXnMuzsp6wTMzSvJ0ka8KcFGpfAQuvLBqhAhtG26kS2v6uhY0vvi3gakkUfYNQ2IO8yjz9QYyoCXigFvKLRiKJyCbV10"
        "ty7zwB3JuMc22eRfJkeOgVNF03n//f8wRm0ZWtlwOR8DtHCzVPDPA1tiIzBMirPCSYgvNRUlL1nNdQp/N1Ggm8y/lGowT+lf"
        "OKRcZMQm03/hzDsbTRjbjrA7JnfRUUZ4YCKVGjpWn92q0GYGwTOPmjq1xJPkotLooAkUyyMoGnJDyOizeigfi704YXx4wfAn"
        "9ZCf8uGboZskEBiBTVNHmgP18+V3g1jSAXrMT7e38pJ/eGELEkOiLh+gD/yBtBHY7HU6dxYeS0rSPTfE8eW6erbyEtB9i10a"
        "oSWAOOK0B7s15PCpeUivNNzV+eEK5abkRJI0zRRiVoQUaRGGgCDkCJM0JeBmKU9zFuum2WfktrkJw0fVXe+naeePw2AcjT7v"
        "vqWdofvx+ib7Vzj+uoPkaDsIEtp5jn840vp7Xer9rBfdfg9Qjr8/mYTm6Rlvs/n+BxwFCtNb1YZT//cDWJixUbvpx2DK8coo"
        "91a6SQx6/gWnTnDFUfvZ9NMOPl4HU/PlfuUPeNuLPzHnEEJMuvhMv/MaHvEtdwk0KB58RjHQOuAnDmLseGP4J25CRDAgTzHs"
        "+09ZVpcaSvLi9ub003q7ubkFoTOpr63xKazsAqpd8+waT0jfrkVZN497fHy/0uxdDz+hPcoEQqchbX4KBzsgCQANGcUvsNxA"
        "g+aBkB1KyVzeNWTOwjZ06SQJq5sffkIHEKMThIJ87Ho3EXzkE3a5sv4H53yOzSh7dk5NCBo+qDQch3dhxj89iVhCaNFdxCTn"
        "Tr0/rMtaNHn8Y02lePW+AFnUKoBgChzIOdvwOg69D2+Z+fIzBJ7x2iziUZVJuoZUHtGQu3WjKmGTcXh5+mlB7byKdCSooSoC"
        "snCw+hEVTTXQfG+t2eqEYz7qZ8GyUZf67QGCnrSCVtDumAdD7E+CfGfBaO2af7E79RctDmhLfgeZ13qKHWw9Bf++7gX1dmez"
        "sbXd6OCiuObGti/JJCYpsycjKfez1tOCWaXc8nax5Taa9fKWX3QaG2i53W40O52vbFmN+ZlpeQsN46CJafr5i8bzVqPTedFg"
        "upH5ltVqDLk8kJBfDHtc2zFuTQJcPgk2Xgz7LdlsEEE86G/2toYDPgDtkkqDZ4P2sKVa6QcJCkm3na2thvnXbG1u+WoXkQyi"
        "8r28Bq+rfLulB4n3azeVJV5I/V42qXz73Lytrt3u6PZ7GHwbi0if6GBJN4POA+W2zYBvggERueV11ObjlxRvNfhfc0MtS280"
        "A+nh7zqpk64syXgA3k+2e1v94bMd+0hgmYJTkORA0N7YGoSIjX7yvL8RhOCJT/qtzrPBhnQAj71pbDjUCIH0WnZ02xhXZ9MM"
        "zgXzTbP2goBVHb9oqX7RcMPABwGLj1pBb9j3NbTExLcBTubqZUGAHig90mzYfh0kQL/o+H4B3Tvaf3UGKxSEbfoueBcDDSsT"
        "UKh4jCQdkHAzmrIB6mPxWS0igcx4rQkguRC2Z6eSGp7OeggS8JB8IF1KpTRF3LQUUTimR47pgWOyLhy/OPK/nDBuWsL4uPou"
        "fWwVSeNXDEAo5JuNN603z3cW0scng+3+dhh47SbmVyQm4KmAXUCTJLHY1lvrkJfWYWuzve2Sl81Xm5tb2w55edVqd54duuSl"
        "s9lubbxxycuTN6/ebL55k9MTZ9BCQQTGc3Lx5E37cPvNviUQ5rWmB0/QWOv5QU4BOs0ti9vOmAXrO1VvDB4RmvmvVagmuC0s"
        "v4jVh4cb+1uvHsBqg1xqjPzwfH9rs+1tbhG/VBNlDNd9l5B8AwPbKAxuOVoj+43uWnq0vXlb0rXaqAJqqznel9GriXU2ktzC"
        "sXiV1boA1IjXmZbfYWdhkQQlMEXQgYAiczp11ccRFaUNAukGSWphAEoCW0Rdjk/gvGUhngcSGcohJYGKeoIXWLxl19AMRwx1"
        "/DxBCudFdEY1VZC0RGr2tNQsePmgDPQtqUvn11GXCuHraylMq59DXzWNef6MtG9j25VnWttGnulsPm+0n2018H9ziwKNegrG"
        "AZ71oi1M1dvYspLV82eN9nYD4hYYPa5bEc7kkq3nW0XpaiEiQKZ63n4jMtuT9qDV23imcaGz33q1uS/ahitNDXuhpjSG3PVf"
        "9DYGQ4fcKdxyyd2wM+gbDu1KU63GNnlj83nnmwtSznq2O0vlqGfzclS+7O3tJVKU00dn+3FCVHtehtoUMbckQxEEyHM27YvS"
        "lpclrNa8hPWbaPFGK6fF3rMCddQ02PajQGinJH21HNnLWdAHJa+cRJdHtLV8RNXCWEs070ok84t0XZEzUYO73WAIuiW0TcUj"
        "kSbUdhzlGBSb5wdAcEGB8HZti1p5olR09aXHYIexCC9Gd9/sPOVgcy15TbLbz69CedkAX4SrkmyCPa/2eS/Y5Ue3Whoks1BC"
        "EuB+bnZox3DYElxZ4DDCr+pbVAh8nEqiPe8f6s866NV3Sq8pk2NXL43sG5Kc3kbZ2jhIbxcthjM6bDpTj5C8wSry9XVW5qwO"
        "DBvNMsYVFvlck7ddffG0oQfWkFGIiSN68HqypryqHuE/THY8ZeDZ5MJMEb4jdp221T7E8INcMcg2PchumBKNPGAnN+94PFVD"
        "xv1EEmN/yaGFkqanq6lC8ADSpqVtKwrktfkmoetW5Zg2ViFVynJJn8Ca0WjDu5plmE6JUVppHtKI6rub5YAGvRCQTRBH7VYu"
        "PNZGKGdHdmS7cosWaLg7BxxOwxQKtithNhgfp6HBsS0i7fyUwC7IzdGc3EidcDVGtFq15SFxPZ3fWNnD4hbKN22fI1jNsOUv"
        "XryQp3PLlAqkq9LW9DXo+E6fGA5lRgVCzJOfQ5Dts6Vlbdl5d7JtPqhcEtu+kh2lh1EXbmaY7Xa0J6przHVOV6KmdhbPUXr/"
        "qLeINtPi4j92PE0Z0vyCWZ5UNlMazqKaQvbKR8/oOWe08VtmtOJVz+lr9hvBvlko4BhS3P6YBFMzk9+4FhDLBy7k8vuO/F5j"
        "Ak3qBmuoPhtPMOf2MOE/DdmqOXz0XRq1aWhUZ/PRRKpJAa5yFnxRsSb+3HaoN0lhKMZoUlRp9FrLN8uUhMEXCSFlIIpEDn1a"
        "XKqE+fzNDP/qSheGAXEFDVVXqwDzu16YlqwBvpDKytKr8igKA8Ga0Jibjmu9pyRkoM+M5GYBOYasStpFurnm8Fg5L99HEvwS"
        "eFDglmV5PJdqKTcEhticuLAUTYQkK5ASh3S5vl4ABSqGdHeqiCSln4IA8TVwPkdkymskOzDiCWfN5x4mSQXEpwungmlsaKZB"
        "JST9OixrPh+W11eaqcQSvlhAOUrL2A9G/brBFcQPiBHVQZmWJeBz8Cm9N29LCyQ8ZAlsLYDGRVRedXJX7GSjNUdpt2XBiwy/"
        "veU2gSC6yXVpsFvVFLuSus47l2SW+WOEsiDRTJTmBE2x2Y67W03ESD8apH7rOCiehNfzcohQI7bW9VSbAlbaeeiYiB5iilrk"
        "wX+tAnxsb1ogECpRIYOIkVNbjR8AyWfKCPUV3FEgT+tQeT2A2lbayBUFfE/LE+65i8VU67cPcfSN+VVLcXHraFFDjxG1HCPd"
        "Dfe1EsfFElEu/1uY/zxPNKp2VS9mkrq1aHIDaxWAJCZiIwO/19xWtXBDG44tPwBJGnC2CnR/o/NthONcoYazP6ASuTO3N/lI"
        "o4J/OlfBeaGtEteVMr7p6E2dToFfdRbwq4pRm113QbbZSZ0BPWZLC4U5ATXEzrPqpSpKfMrKvHR7lqIyYVmHHjyKuczhUyUb"
        "x6i+DXbTJLcYi8QkZ9ehibQiIN4ImJFvMNX+i2FT3r6sh4oIsVKushEXJZVOUXPsbC5XTNnB9Q1ONxGe5/WxOQ6kqlwAMYLe"
        "KBxcolqOy5tbi4IxdE8f4+Q2Ev5ahW3zPMs23fa+U5dUBFTZi61ZE9nDBrI8gMTdrgUmqoLdZt5+uln09WMXggnAQnWZ3lCy"
        "SBlnymhtWYF/hdB8nZM3JamTq+ar/Sr/gMu5xdrt3YOiLCxky2BBbrDsjqy/tilSxWblJi5Ulhnq9ACSqw7Wcjo31/o8J6zQ"
        "FEx3FumEePDixEQv4Yyp6rk1tuyFAotLl2la8EL4/DT9KiHGjWTqbGvgZjMuKmwaCeTRHGaJVP3ojZBRlCXAzbkaQ03VxVTH"
        "hO7R8POaRQJDgkn2JRzxq2yI88KzaCtobCBxZUY7b7k8rXKl5njak06nv7UVFqlfSzkZjIF8Y7PRfvG88WJTuxiUEGl2W0zl"
        "DKX09GgWUV9/aSeu/bmIv7iXkDeULERg9f6L+M5cCrixpdARYYYwPV2XQudg1Mi1VVmqnOQpo0ZBlkcjawpodWSdmBkRW7e1"
        "5c2itTFvHJhKpgCcjAWJCxiVbB7OGZDEA+bvfFM98FlJD6TXU8Ep4IRzA6wtD+Qr6baV6r47YtczRXeZDQR84fa5lFKYcRnz"
        "kWNrEq+BbLWcq0MS8vydeE19lDfGr8V6uIIBoF3S/3q7/b2qZ9R212AszytV1OdGe8TBxUdTQaF5lcOxLVUbrjPyfjZE2C9B"
        "sAvkm9t3N78BrhZC6v2KjMClQsrLJY32Geg+xQWFnvlUqYtlNwXIBPHs337WArwbYdreWWJgXGrdc+Vhitrzuh1w9KuMIAtt"
        "IBYFXxg3j43mVZ6/ohugFO8hS1Gg0CYEaN4rOV+2HbR6HUGtbOCyzq0FQyk5pWHfbZh/zTb964AoXlY0Mms3BjKPdAdN5FJ4"
        "lEembFsQf6hVxTY0nqNBnogxXqpnJvTjYfJaKdRUmmIs/TAiRljU+8ten0UYKfWiyZQ3C2QD9amMAkXzm5qUVbofE4K9UD+e"
        "t8A8Lyga9HgIQUF6CXUQ3RLcrG8H6yzzxuOW2UxvY9vsmGqsO4z7ItLkWJ9PzlFcTflmLxjMlX4SDjfxUyUkdHIh4UXj2Tb/"
        "Z3yvTGjMLBQPWb3KFKeAF88r5KTn1fLkYhW3aMi0I3ucHsuSuKh8VFl60cRDZuxxcVzhUZXiv0i0Jbm0+6AC/gYBDlUM5rcO"
        "PCgLyqb1jWVys6rQKxMIIyeLxv9oe3rzGSzqrkHdsGdpZo8JGBZw2kUunGeVFnJproggFmUfYYb+xlj9gIhH/qLMkvN4jhMo"
        "MC6F6Vebkj8+0vCnLFVWhH62UNlwgEtxmoLH30V2kUTLKK/lSwP16UdtXaugNMNCVAlPiuAUG64cbGt/ezzNvl7tmpu/3P8N"
        "Pz2gyhVk0XYzW2yNNhijghPyCo+yPKtKCxDtCbH4R2VxeMwpne3fJuhvbeUmNBNgvyisxDWFCctYZJCyc1imLCzVAzQ5eUgP"
        "4EmqpiTk9BjJ0/CcB096f8IBunLPJWOX24DWPTQ+KjNMW7PGl+v67NfLdX2kTg5m9cFScRWFcyap5sld8nzCU23yCKVJ0HRh"
        "hp7V9l6u45E+oAceod/xcqE97MDLaHwtJ+7klB3PaJoTd0xGgLsvpJDbKGNq0KrzSI7vlg73sU+3DGJwansZjx+o1FzXNtNw"
        "QyWPlguimRIx9v75/7KXIeiG1BSKA1FKTc15qX0y+j3iI9RZQvkg0LWLCx+Yds+9H2jAjBzIbhHkFzTIha76vmgEHvVhyNE3"
        "HpjDuLW9/+9/+5/+R6SlHPMma93Ey3U1gPJAJXrFjmBfx0+rDBNmGLLMqMaYnj1dBJDAb+qFnlvFlp/JX9t5RUmqB7W9I/xe"
        "Vko0g9reMf8UpqLhZ10BEM94BpFdZYKygpFUcUvzQvRyxjWYqd109mR6Fmpqe209xTwRY57QG/11dE1303Gadk+HKBbACzpl"
        "EShva3vnpTSpeRLxefi8Y/bSawUw2ehYIPrv//Q/W+Ca7xLeXVP8LVIHOMULlUpQCeFN1eq91wd6DWC81/d9zF+K4wwcvfz7"
        "/+btIwdJnEwCd5vsRvGD3ovl+yLDQM799ACPluxSx+zSqZxOxxbBNBkWdmhqitOAXdt7O0HsCjPU7c5dn1CaWR1XfiK/JA5R"
        "vdja1pdxeuZZt7OB3K9//8//gUY08KAFFzWYkNW//6f/k1lop+U7HZre6QwpWBRaA9dw7n3CVI449cxsXC97e1iE5l+x/1jP"
        "vebL9ek83FljiVo1+WR3WF7uvcyEXr/MEn7ce4IDzjfy6e3EfjyZZfazJP8I7NdjXoVYV/kIfOepU0R9WGcH66qzHDAz4RIc"
        "m2T/Zwk+4V8ZXRFCSpOjlaj2IMAik4MF1mPkSY11nj8m/s50ZhvaWdUFqrMR9jr2a3vfC7jKfQFSvgi0S/o7woV4DnaA1kZh"
        "4bI+uTHa7J2Q5H/nAZZ5RbKzo4/r7AzZ2WxnZ8HoLnC7knsqTZtMRCAPOW1+eI0ro3l7n88x/Mf/11PVHz8ET3x3eiAHOHye"
        "5CORKxecoYDey6O5Vl2kzVljzkFKRUTJ0vRLPpYLK12GR/5xGp/UVZ3Ll09U76YQDmR+DeYdYA5bEJl12Qnt+ft1VGkxHdow"
        "dEjSRJAEVvMISnQFJrH3UsK7FIqV6JDOIftyXRXR01ZkXeQa2kKRYuI6u9mt4WRCTQm9N/EIvHBXLjCgu6GPzKMvTaN7ko4X"
        "8bnoCjSnf8t8BnA11EqcIR+a3e78tmJ9yQZTBmK42OvIkZlUlnk4L5xMIHfMZilpQyTHoOTMjXnJiGSVilWaTiLkmVybKCnI"
        "8hsOzALkFCVfCX3HQ3HmrZpSd5HCGAOHzw9ul1Fhhu5H1fgBpX+Vn1MnXpFUnUi2kEHBKwyhwGm1Sqo5lkqXsTfXvCkOBcni"
        "zwmmHnpB1ZYXk3E7OZmDSG6YUyn5mXZYsw5JkiMppnXm60UkU8VnaHzWlw8icUto3wDMMHz+W4LE4KNgTQduhxQEptF//3/I"
        "p16KArj3cELxbvFmMZlQqgTXwvVZkvoaKCbNalQrbsk84ULwgmEOkodkjzxY0NMVY4rS5NdQARcIVDKWIlWA39ASLvWCgzFF"
        "cZMLYLxZeL+cQi4nzdTyLGQd3EQzYF3h6tQ9eRh5RVXIiNwOvHLxrXagpU5+QCG8Kq3Ty3UK3Fq1Y0mj81pCe0SZJhCBBkpU"
        "zniU4G6qAY1rFkeU+8y24K50X8RBUuAC4CCbDxShWWqp7svp3qkkx+aNdskoZkIu73XwE668sLJZl+LVzwjxkAWHiPZ/e2d9"
        "3NKDnLr8/AFyO3yu8rm0aEoQKwjUL1OpurdSm/G6dtxfhAOUOysgYIh8+Z1clO3twqQHmzKpG5NJHY5Cfnz1GRdbRoxtVoWR"
        "YYlJcFn6DM3gbp40v+h+/eL3L/dql+uIHumzRP1L7fe8nvb3yGYvV7+9lG+jTL7syZdr+bJaW+WXv4FB79TuL/qXPBMGBxBS"
        "kDBT22w04hrBXsJUcsISkL4IuceP4mv73qTBbplXfblsp2WGTuLNS3thghj4HB6uoMbt7vj6Bbl3bmJYXWqnJ2fnNXUguOv9"
        "67OTY7nsCRLY8DPus7334TRlnbrU/3Jvl2WIEbBteX7S4zVITYAE7Fz1L1QSu/naikeJ2iM4g3iWkFVgIoaM39VF40YnBCde"
        "qNGUW9JhUarjVmPcNCvJsUZyxxK6Hs4mighMZ7wplBco0DDjTv41bDLqIgOJCEKKsHXwjHUypUZpnJwCM0Opixj+8R85PbF2"
        "2W7640Ed2fPkjjxgDBJnybk8Xp2Ibfr977FZKioBH7/TBVQOKVqsFgwLb5yBoQsM7Av+wCwmXXVVf2JTU0O8l0p5r6arL7Z/"
        "3CvHvPaMoDmIx2OmCTTf6zIlZ1Jo+Ii3FY/UZIrbJBtxrO4TUBYk5vwa7cwVdPeTt2UoE10iqcdS2ijjYV4DLCb5fCbmzDjZ"
        "x5RqhXM6vIup58AAciUjr5JAACCz1NUuOpPDdHZCSADFbFB1LsjCH9zCJkfcDOmg0WaQSBp4Vg5KRhbPSH893MkeqLOYmFY0"
        "VmkUvbdjuZhO9mUMazFleosOX3RSe9Yg8bDr0FfXIChCg6uKg8ldILdCE6f7JEIHDOb5hHe4ZU5nB+03xRApJZQpHB/hPN1x"
        "eiEVeMeL4OFsoIMVOjWGJfUanv2uautmr+GZCT6qeURjZLBnsTVbDWmTJfNyx32ju89fuaUbpRKqG6A4E3T5TSbQkjlkMZAh"
        "+PD+CHdMs/f1KYwpUvpeLyfDHgB+yrZY2OoiiDvIKME33xE2FI30VXQgloZpyTAIskxgwlGTy3ugQqZ444DU5HMwDFCa83gq"
        "FNShtaYI8zWjIS0x+PMNCSb+0au/+nD25wv9/ZJ0Bffw3MVJzLz+vse0bzo+a6ca/Qo4IPnjGt533+kG/RKiXYBoRAjLBLpp"
        "6sZs+BEudXx1fuz7GhZ/R47WNEGcGK5tj/SkLyunH+w4FeaHo0MwMSJLelC1r7YPOxUgb0Pfy+l0TPolg0DSxwJiMEVl8BFZ"
        "gjVX0nSax3eFIAaQo7u8cRw2bGgCtXsNUMIfVcVEZJe6fmG54Q63iRIMgE5EFtqWkIx1x957+53irAQSy2OlhCWO3CNDcriD"
        "2oLq71TxrAK90jWFw8m6CvP2d6QnRYxVqsECsXaemW8asJlwjwOdn5M0LEOm9RKqnubSH+GEiT82JT8f+RvyU9hbT1T3955w"
        "daiGsgTo6Pvv1fLteRv+4q5Y1+mOu8sJu5zwj3ARtzxx+Ssq/Zt4QjyhUf02p6x2g4psgcyeIgGXd2cFtP61uhNCU/sDqsFi"
        "hklJ6Kkc8+w1xxH5cMGofNt0GwH+VFZapMGGqIpEJUhEq83w4ATRXaBzbbKpFa4cuQGI2RlMMf2bU9xYNU7rcK+JjQOQwKdY"
        "yAACjXh0OC+7BuaD5ggOSPEqBlNhZ0XvKB4e0qnFEjSr1pENHolzr4kwoVoejkhudAq4KeqTWiG7cu5DLBeatyA9v9zLob24"
        "7LmEobN6f/k6UiWYqQiPHojvFb7acYlcpiih4f1KrCK1kYP2qkdSYmbE1DkwQU54mXxcYTcnbYb0Rc206+mLWdxSuVldlSRW"
        "oqAocULY741gjLkUu7TW+7x9rQLbZpS6aJv48fAf2IS6/Aavnxy+ONh8to/yr3DtIR9svH7+6pDXYh58DiZ80Dk82Hz9DA9+"
        "SMJQnmy82t/fOqgxufdA2kDOuI03+H7KKGw+2N56s7/NB7NkOpJmtw+3XrBZrOUb5CdPo4BPDzobb16xs/dxKsUOn21vvniF"
        "B+BrQrL4cH/7xaYM6ez2s7T1+mD78BDf30USof/k+fPXz/a3WCscxxM1gsOt55wW8oDJGA829+UB+j+I+7H0jgwcnQ3OjHdI"
        "jfnkTedw+2A7X66354XVuo54vUe+WAjOyxcKuu8kdpYJuu0gtIuU4AqpOF8ifA3c9ZHURXiQr81w1scHGbBeHNbx6OLrx8UF"
        "GtHzNwjsAiG6C9QmzBeIFCBwlmcEm7jk/9GrA97dY192acAig9hZGEiX44DLQjXy/cnPZ/qyQHO7krlNiboFrkimRLnWZlKh"
        "uzDXOfu0Ke8nY1eP1CqYRb/6F7n8V/rgVXg0fooyp+2gfvOO93M29HWyXc1QqIPZV9q7r1Ifr5gb2qSotoMt0QqtFsqxn8Xx"
        "xKFYMgHDoWRuoE5mjnO8UpGvBpmWImKOWoE7SHIxcmhueX8jl8papgdZtclMzobBoiBuCwQbHIIJdrYK6iTTQPNO8WV6imgq"
        "2tKKBfSOg2Mx3o5y663YH6mTcJ+5Uev/9i/pH+p/GXxpNzr3fpefnL8Xjealerlx76Pg79ab4aeQ93yLZFOzyuXYKK/e92Mk"
        "3tcXW0P9w9fOpdxqrb5sXKq/m0j7Hwx4WcMGrButGpUChCXK0a1HjKu78y82LgxE1CF74biYD/LbZ4Jjcgm1szdyt1eqxQur"
        "SAWfEI1CNUYDwI15g+DnOKmn6ELf+z2ef5Pf//1J2uZnp//6DQDkhumwu1SvtV1p7HM5Ve7/jl5QBMP6ttwn8N43jLmpt52i"
        "m6poCXqBoT8ijIR3AUcLAQ6gBrNvEvV4GzG13rsouY4lDBlCyhRONF4v1AcajEQiyuh1tsqwxKcC8ZCmcnoj2LT6EtGXxibd"
        "LzrZWbpmnCYSS4B8yquYFS/TWa15QhfUE3aZXAwvffWm4Evg+ymWTkTk1WrPSdE0y8BHHQAzvSl0qc2zp5S7owUeY9ip//N/"
        "KBlm4Sgd7K06+4l5J6qHSHUQSQd7znIgjLm2x1f1SC6ZZQHVDi9jCCn/RlQfasqj3UIcVIsAYF7yhmT7tm3fqmVw+kF0c00N"
        "8CX/FZdbeanKKy2Je6rWmjdGl4as2151ep7rhCn5KzphWvuKThAKUdnoPOgs3FZIdGYryw59+nP+/k//BeIbthQqR5ppV7T4"
        "H+y+5l3Pd0JDkO5okLvfzmNqEaUW/9f/pQpSxA2/WrBj0PTyHjxUC8lgeuKK95sRUpUnOpk62WtT+RNBLuTbOJjWNWb7Ogm/"
        "vu8A/F+FEwzIU+mp2F19vqrnsSpxvKt7x+riIRkuQh0gL0Q0ule43fcaJsLCREGYGAjjMmjmc6vJ2SPiYL1omdPPlvM7UqHc"
        "M4lQSTK+ASMemHGWDudQ87pMroxpMSH3QMknBf0COAjVIl/LeQ0zSy4Ull7WrGkkt0ZwhS++zxKrWUYQnuQybPLtBHsDYt/L"
        "vwMlfdcIFwViRij2W69daMSIJpc09UW9paXQ6KWx/EVBlUEoGIg9iOOhDBLBXHtcD3xTp7e0Dh3CqFRXtXBfIlgtzCG6Db5S"
        "d0v6fmFqYuMtFPvOtiB19rxgR19tWp4ZD1zMmcvQ4B/Jees9WBYD8jkhBLnlgxor9xvXUEiRHXWzF1/JzDFutQMEhu+/V/Yn"
        "wSQVo7AMlWqAefJT9yl4LMFeX6uBsEIVgKhLcqRo1zfFappe1AmJaJAVlNNQIjJ3V5V4q48brEobLIrqdLqnVODHErkmPsUa"
        "5y/cvib2zhyG51V+IbZW4bfMmPJpiINauHUwswA85Ill94XYhOh7yJJcvvpuyNX8Lktc50UFMoAju20JhXcx30rjQgWUXWHh"
        "NEShn59Gr3K0xiT0uOn0KqajMRSVC3MydXJrEiUZmhfyqzFgjEwkchwOtygcQRVzS983xHIovVsIrTEmFkBqC+o25VaNhlrb"
        "6BIQWllZ+E2x9hcFqymjZ0JeDNtmMinLRBwzjFl4CcGqsOjYtaCB2xA9FxHWvPalAYCmNPuFx1ml+B/ljxCRrly4g0/qg1IB"
        "5RP4PD8pe23FGPPdmKTKNv8oeu3Wm8qlTynuYUrdYZcojwwZ1MmOGYhaILOcRIE6CzhMpk05alRXxl6J3/ML7oHy0x/F6QGY"
        "VyuvwrKq114A1FluF0b5LvcTYkZWF9+jftryjXar1HPX0+l2WyS0tTPeXB3/0TtgdLLEp+DEvSa0c+rwl6UtSTQZMi3cN9R4"
        "ikZla3GohsxHNLziGhHpUfvXNO+zsAIwxbIpCQx2jEfBoyQgfmvRzqW0pEhwLdyL3Ta1f/6vpM2SuhLEGTcRUbtG/AIkHbaq"
        "BJK4WSvOVVsfcJUQ2tiHh/BzM0rlL5zmgLMBQKs+YG4cWnVSEqSLy5xCsaIFgK8fnlywxfijVdPBanmAejOkIwqN9VT0QDHe"
        "EJ1NKINmpTXSVkFm+4IIY94o7LavZDj2neB7/g5f9bt738540DSW1Cu1kXPGIrpJi4UWk48l66ViOqHNVvB3zdvBf8ur2jRc"
        "VwWSzqMuvZgMZ6RNKlJibv4IhW94D5prhLI8OS/GP7zTcEdWZOh7Qxl8HXO4wb1UGWvmQA9mo/QyhTq6DbNUzHeiqY1Eoy4j"
        "9ESQL4WVrbDVccuYgIP+7wENhAquuo7akQNPIpxLwEVkMAMgSpM0IKFUPgDBfU66gyXe8EDR38B4iuFU0UWUt4AeYzpbXo3i"
        "Xv2iHKrS0PbLjg+h/YvcoqtuvCqEm2qQDOB5+Wh89jUbFoyDWvn+LgUzG9+bMn43i70FYb5NRdSEZCXB9bHSWha6c1gm1Gnr"
        "tUMnRPIhOcn0OhwGCJSuK8Z/fQyZlyO0AV4V3jr/ga5Aje9oYDZQS7hcW1MDhRIgDCcf9qLelOP0/uHuGEhmJjY/reW1cQTJ"
        "kRUXLsrDAy0JzOT+53KaL0xyDNVU6ytwVMSugnovAayOgq9jUYt6CQzI6rmgmISQ1YomiNX0I2kV39HRLqZ8KhyMnVZKAyjY"
        "qjJP9HdXWbavHilDxapRVd6eI8hMvdE6inP6jtXgdTIl8mjTWm5m0CTSziOnOKFL9KoleJyppIlSyedW6LGuiVyi7u8UVq/g"
        "YHR9A3O9V8FEtTNhoQPTpYoVGosQeYsuqp5iBNa58fhaFlZUWIBAiWbaJviAYrGWHHIKXAq1Izl+SBBHMivft+lhFNTJUstX"
        "8mrtGNJEb44x6/LquWLuumjRq5MXVc8LRZdvhI5wYV1VUAZs/VR4ZY/opOHfjGiQQ4kjKMzHC+VIqI9rVQTv0I8jRgn9Waba"
        "za88LfmL87WSA12PaJBpyVMRQ+T2VHVfM8wG4gCwfiQpwL94szjmHIH00ZCMLagtvc46/xFmmESOH7vg3s7P0tVyCjktrbwh"
        "i9Mb7oHEtqgNWmTgY8xdpVGdt1qraArGnCOmQrNP/taKid541VduFhJ1dUrHzsdLoxab7/wrGvqlqpj1F+nIylYkxMiEVQmI"
        "Krd8MVDKFSJpV7BbS0DTkQ47PIqrA45BOnmYiocg5fpL3IiJWzr/9OHtIR2QBbuCsgf4ci0yQvEDeiGAF/AeBxMcdqjbuJQB"
        "91qOQ9BNwVgyc38pEGY2NdeXoivYdKEHBYUI6a6YnHiSlkdJHx0X0+TI2ewVOd4VesKtvtepvoAa1nua8VKoXfB8CY+4Oj2S"
        "K0VVz0ao8s3V5syhyMvNERkejOEMdwbRtaensIaIlQhWnCuKTR/hJ6xNWr/CraxXb94eHfpyYXzpLYbldxdfNIwzFTcwbIOV"
        "oyAGK/IINGKerTZXkPq8Unw4fwGzujQ1XwcxgnNpICd81Z3D7q24NYa432Hbc+Xk/PD48P0hb2d/5MlI39rqi9SgttB07+Vn"
        "L+SMx/zhjq43f1rEXtDrr8wtbn7RrIaPxeug7l0W6OVhBQO8GpJVINxMrqit19s4RtXwtp/RzRohJ9DuFX/rYOxdQSpigmpn"
        "5f8HEl/tzA=="),
    "subfinder": (
        "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"
        "kG9sNjv40A0ftckSgxiRerY5VE55oJaCZpX0dW/sxIw/l/kX+7bQF5wrGVjeX/HamSkBN7aEHZFmiNDTqJI6h6BG4a0yqgqR"
        "J0GNki2PRlpCtCqzjsOMyK3b2rIWUQvrpTGX5H2yaM9UaLb84VIAiVfA3N0v6gduV/xAWvUUOgWd0NhAazcn8lV821p334TY"
        "XJmi5bI8EfCR2eeNkkLDpcNHRqyJVw14qqfIWscXNMY7XjV1UV4Hv1b74UIDjbsUNDp4tJN77Bzvr32VJsGnLwWoejoSYMag"
        "+Xmt1/swd1n8hE3zOpVdvI9m2AbLlAvE9UK3udVvmil3URUr6JVt+Nwj/KzAxaYySuVqS3hykZHaMSjGHGJvGAcsSlVPJYA/"
        "wUviFmk/vxWOhNjmMgusbFmM1tkBf3Boka9iBcpIcrn08AbDLKbjye6oOCUuWktv0tKnEOejqvmwUa9zlmwQ3i4KyxaIk5bH"
        "YRWOpKLDzXKMUEcvzLKRQbXbJW1WU7riOmmbGJohzYV9S16qZTLTnZdlohVa/7w/yJfLtikHhTym2+W8Qc16ia+qgItnFWZZ"
        "pQowopl/cefQI43U+FXE+sXKWUUu1IXyK5dItxxb1GIZefULNnNXLSqa7WxX4pEdsU7qvNT/rLVGGsEigXAgWilFSv/u0Vmt"
        "CFT/XhbkaMwTzO9AbHWm/GrvMu/ssqJctus5fYX5s+wY5nSZz018aU4Vx3gDOr4JAaJdsxQt/LN5jDYzbFA0IUDBt+ef4cOX"
        "ZqPsfxeBXfr4hr8Au1lC0gZs4vWUo/h59RVryVGaKZshX7PfvhhXvtfo1hqdv9rwu0l+RNmdxccmK+A26+LK8kU5Eiw+U1Ub"
        "1KOviBjr5JdKMhTWFZv6n9fZ1FDfLZaKr/g/bwFVGlhJd8vVta8HvHqzO68NmMKOM4KMzj3sUK8vs5tjtFt511lZRX2G1FAN"
        "kmRYwfZLa0kh7Wpleuci4+sk+godq/yynm5pyTuoMfy3N7XhLzrxTkqQXIBC1tevzlrabKhCoT6/wnj7ISWhxDiH1Xhwv//9"
        "osxeVK8SITMbUH6IQpvEbjoq4vF4XX0w9nhdfYTHX3PxrvB7tvEhk807rbfoyZ7e3h+lcUidLkz5avb+43U8Up/0gZHUO9qC"
        "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"),
}

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

PAGE_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>PlutoniCut</title>
<link rel="icon" id="fav" href="/logo">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Rubik+Mono+One&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root { --r: 16px; --gap: 16px; }
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body { font: 14px/1.45 var(--font); color: var(--fg); background: var(--bg); }
.bgfx { position: fixed; inset: 0; z-index: -1; pointer-events: none; background: var(--bgfx); overflow: hidden; }

/* ---------- aspetto STUDIO: scuro, vetro, bagliori viola ---------- */
body.look-studio {
  --font: "Inter", "Segoe UI", system-ui, sans-serif; --hfont: 700 12px "Inter", "Segoe UI", sans-serif;
  --titlefont: 800 30px "Inter", "Segoe UI", sans-serif; --titlels: -.02em; --ctafont: "Inter", "Segoe UI", sans-serif;
  --bg: #0a0a12;
  --bgfx: radial-gradient(900px 500px at 0% -10%, rgba(124,58,237,.38), transparent 60%),
          radial-gradient(800px 500px at 110% 0%, rgba(192,38,211,.22), transparent 60%),
          radial-gradient(900px 600px at 50% 120%, rgba(79,70,229,.25), transparent 60%);
  --fg: #ece9fb; --muted: #a39fc0; --head: #c4b5fd; --logfg: #d6d1f0;
  --card: rgba(255,255,255,.045); --tile: rgba(255,255,255,.04); --seg: rgba(255,255,255,.05);
  --seg-h: rgba(255,255,255,.09); --btn: rgba(255,255,255,.07); --btn-h: rgba(255,255,255,.12);
  --bd: 1px solid rgba(255,255,255,.09); --bd2: 1px solid rgba(255,255,255,.08);
  --shadow: 0 20px 50px rgba(0,0,0,.35); --blur: blur(14px);
  --accent: #8b5cf6; --accent-bg: linear-gradient(135deg, #7c3aed, #c026d3); --onaccent: #fff;
  --onshadow: 0 8px 24px rgba(124,58,237,.45);
  --titlebg: linear-gradient(90deg, #fff, #c4b5fd 60%, #f0abfc); --logofx: drop-shadow(0 6px 18px rgba(192,38,211,.45));
}
/* ---------- aspetto LABS: carta, bordi pieni, ombre nette, tramonto ---------- */
body.look-labs {
  --r: 10px;
  --font: "Inter", "Public Sans", "Segoe UI", sans-serif; --hfont: 400 12px "Rubik Mono One", "Impact", sans-serif;
  --titlefont: 400 30px "Rubik Mono One", "Impact", sans-serif; --titlels: 0; --ctafont: "Rubik Mono One", "Impact", sans-serif;
  --bg: #F3F0F7; --bgfx: radial-gradient(#d8c8ea 1.2px, transparent 1.4px) 0 0 / 18px 18px;
  --fg: #0E0418; --muted: #4B4458; --head: #B0126E; --logfg: #24103F;
  --card: #FBF4FF; --tile: #F3F0F7; --seg: #fff; --seg-h: #F1E8FA; --btn: #fff; --btn-h: #F4F07C;
  --bd: 2.5px solid #0E0418; --bd2: 2px solid #0E0418; --shadow: 6px 6px 0 #0E0418; --blur: none;
  --accent: #EE3A5B; --accent-bg: linear-gradient(90deg, #F4F07C, #F7A541 45%, #EE3A5B); --onaccent: #0E0418;
  --onshadow: 3px 3px 0 #0E0418;
  --titlebg: linear-gradient(180deg, #F7A541, #EE3A5B 55%, #B0126E); --logofx: none;
}
body.look-labs .btn { box-shadow: 3px 3px 0 #0E0418; }
body.look-labs .btn:active, body.look-labs .seg button:active { transform: translate(3px, 3px); box-shadow: none; }
/* ---------- aspetto NOTTE: notte viola, tramonto all'orizzonte, griglia synth ---------- */
body.look-notte {
  --font: "Space Grotesk", "Inter", "Segoe UI", sans-serif; --hfont: 400 12px "Rubik Mono One", "Impact", sans-serif;
  --titlefont: 400 32px "Rubik Mono One", "Impact", sans-serif; --titlels: .02em; --ctafont: "Rubik Mono One", "Impact", sans-serif;
  --bg: #0c0418;
  --bgfx: radial-gradient(760px 380px at 50% 108%, rgba(247,165,65,.55), rgba(238,58,91,.35) 35%, rgba(176,18,110,.18) 60%, transparent 75%),
          linear-gradient(180deg, #12071F 0%, #1d0b36 55%, #2A0B4A 100%);
  --fg: #fbefff; --muted: #c9b3df; --head: #F7A541; --logfg: #f2dcff;
  --card: rgba(20,8,38,.72); --tile: rgba(255,255,255,.04); --seg: rgba(255,255,255,.05);
  --seg-h: rgba(247,165,65,.12); --btn: rgba(255,255,255,.06); --btn-h: rgba(238,58,91,.18);
  --bd: 1px solid rgba(247,165,65,.28); --bd2: 1px solid rgba(255,255,255,.1);
  --shadow: 0 24px 60px rgba(0,0,0,.5), 0 0 40px rgba(176,18,110,.18); --blur: blur(10px);
  --accent: #EE3A5B; --accent-bg: linear-gradient(90deg, #F4F07C, #F7A541 30%, #EE3A5B 65%, #B0126E);
  --onaccent: #12071F; --onshadow: 0 0 24px rgba(238,58,91,.5);
  --titlebg: linear-gradient(180deg, #F4F07C, #F7A541 35%, #EE3A5B 65%, #B0126E);
  --logofx: drop-shadow(0 0 16px rgba(247,165,65,.55));
}
body.look-notte .bgfx::after {
  content: ""; position: absolute; left: -50%; right: -50%; bottom: 0; height: 42%;
  background-image: linear-gradient(rgba(238,58,91,.28) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(238,58,91,.28) 1px, transparent 1px);
  background-size: 64px 42px; transform: perspective(520px) rotateX(62deg); transform-origin: bottom;
  -webkit-mask-image: linear-gradient(transparent, #000 75%); mask-image: linear-gradient(transparent, #000 75%);
}

/* ---------- struttura ---------- */
.top { display: flex; align-items: center; gap: 14px; padding: 18px 24px 6px; max-width: 1280px; margin: 0 auto; }
#logo { height: 48px; width: auto; filter: var(--logofx); }
.brand .name { font: var(--titlefont); letter-spacing: var(--titlels); background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; line-height: 1.05; }
.brand .tag { color: var(--muted); font-size: 12.5px; letter-spacing: .04em; }
.spacer { flex: 1; }
.looks { display: flex; gap: 4px; padding: 4px; border-radius: 999px; background: var(--seg); border: var(--bd2); }
.looks span { align-self: center; padding: 0 6px 0 8px; font-size: 11px; color: var(--muted); }
.looks button { all: unset; cursor: pointer; padding: 6px 12px; border-radius: 999px; font-weight: 700; font-size: 12px; color: var(--muted); }
.looks button.on { background: var(--accent-bg); color: var(--onaccent); }
.pin { all: unset; cursor: pointer; padding: 7px 13px; border-radius: 999px; font-weight: 700; font-size: 12px;
  color: var(--muted); background: var(--seg); border: var(--bd2); white-space: nowrap; }
.pin.on { background: var(--accent-bg); color: var(--onaccent); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap); padding: 14px 24px 24px; max-width: 1280px; margin: 0 auto; }
.card { background: var(--card); border: var(--bd); border-radius: var(--r); padding: 16px 18px; box-shadow: var(--shadow);
  backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.wide { grid-column: 1 / -1; }
h2 { margin: 0 0 2px; font: var(--hfont); letter-spacing: .12em; text-transform: uppercase; color: var(--head);
  display: flex; align-items: center; gap: 10px; }
h2 .n { display: inline-grid; place-items: center; width: 24px; height: 24px; border-radius: 50%;
  background: var(--accent-bg); color: var(--onaccent); font-size: 12px; letter-spacing: 0; }
label { font-size: 12px; color: var(--muted); font-weight: 600; letter-spacing: .03em; }
.tiles { display: grid; grid-template-columns: 1fr 1.7fr; gap: 10px; }
.tile { background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 4px); padding: 10px 12px; min-width: 0; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
.tile .v { font-size: 30px; font-weight: 800; line-height: 1.15; }
.tile .v.song { font-size: 15px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 6px 0 2px; }
.tile .sub { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.seg { display: flex; flex-wrap: wrap; gap: 6px; }
.seg button { all: unset; cursor: pointer; flex: 1 1 0; min-width: 84px; text-align: center; padding: 8px 10px;
  border-radius: calc(var(--r) - 6px); background: var(--seg); border: var(--bd2); transition: background .15s, transform .1s; }
.seg button b { display: block; font-weight: 700; font-size: 13px; }
.seg button small { display: block; font-size: 11px; color: var(--muted); }
.seg button:hover { background: var(--seg-h); }
.seg button.on { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.seg button.on small { color: inherit; opacity: .8; }
.switch { all: unset; cursor: pointer; width: 54px; height: 32px; border-radius: 999px; background: var(--seg); border: var(--bd2); position: relative; display: block; }
.switch i { position: absolute; top: 4px; left: 4px; width: 22px; height: 22px; border-radius: 50%; background: var(--muted); transition: .2s; }
.switch.on { background: var(--accent-bg); }
.switch.on i { left: 26px; background: var(--onaccent); }
.btn { all: unset; cursor: pointer; text-align: center; padding: 11px 16px; border-radius: calc(var(--r) - 4px); font-weight: 700;
  background: var(--btn); border: var(--bd2); transition: background .15s, transform .1s; }
.btn:hover { background: var(--btn-h); }
.btn.primary, .btn.cta { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.btn.cta { font-family: var(--ctafont); font-size: 24px; padding: 24px; letter-spacing: .04em; }
.btn.ghost { padding: 6px 12px; font-size: 12px; }
.btn[disabled] { opacity: .45; pointer-events: none; }
.btn.working { position: relative; overflow: hidden; opacity: 1 !important; }
.btn.working::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255,255,255,.4), transparent); animation: sh 1.1s infinite; }
@keyframes sh { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.hint { margin: -4px 0 4px; font-size: 12px; color: var(--muted); }
.link { all: unset; cursor: pointer; margin-top: 4px; font-size: 12px; font-weight: 700; color: var(--head); }
.link:hover { text-decoration: underline; }
.link[hidden] { display: none; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; min-height: 28px; }
.chip { padding: 4px 10px; border-radius: 999px; background: var(--tile); border: var(--bd2); font-size: 12px; color: var(--muted); }
.chip b { font-size: 14px; color: var(--fg); }
.go { justify-content: center; }
.status { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 0 4px rgba(34,197,94,.18); flex: none; }
body.busy .dot { background: var(--accent); box-shadow: 0 0 0 4px rgba(238,58,91,.2); animation: pulse 1s infinite; }
@keyframes pulse { 50% { opacity: .35; } }
pre.log { margin: 0; max-height: 240px; overflow: auto; white-space: pre-wrap; font: 12.5px/1.55 ui-monospace, Consolas, monospace;
  color: var(--logfg); background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 6px); padding: 12px 14px; }
.overlay { position: fixed; inset: 0; display: grid; place-items: center; background: rgba(0,0,0,.6); z-index: 9; }
.overlay[hidden] { display: none; }
.overlay .card { max-width: 520px; }
@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
html.embed .top, html.embed #bQuit { display: none !important; }
html.embed .grid { padding-top: 18px; }

html, body { height: 100%; }
body { display: grid; grid-template-columns: 252px 1fr; overflow: hidden; }
.side { display: flex; flex-direction: column; gap: 4px; padding: 18px 12px 14px; min-height: 0;
  background: var(--card); border-right: var(--bd); backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur); }
.brandbox { display: flex; align-items: center; gap: 10px; padding: 0 6px 14px; }
.brandbox img { height: 40px; width: auto; filter: var(--logofx); }
.brandbox .name { font: var(--titlefont); font-size: 18px; letter-spacing: var(--titlels); background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; line-height: 1; }
.brandbox .tag { font-size: 11px; color: var(--muted); margin-top: 3px; }
.navlbl { font: var(--hfont); font-size: 10.5px; letter-spacing: .14em; text-transform: uppercase; color: var(--head);
  padding: 14px 10px 6px; }
.nav { all: unset; cursor: pointer; display: grid; grid-template-columns: 38px 1fr; gap: 10px; align-items: center;
  padding: 8px 10px; border-radius: calc(var(--r) - 4px); border: 1px solid transparent; transition: background .15s; }
.nav:hover { background: var(--seg-h); }
.nav.on { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
body.look-labs .nav.on { border: var(--bd2); }
.nav b { display: block; font-size: 14px; font-weight: 700; }
.nav small { display: block; font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.nav.on small { color: inherit; opacity: .8; }
.nav .txt { min-width: 0; }
.ico { width: 38px; height: 38px; border-radius: 11px; display: grid; place-items: center; color: #fff; position: relative;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.18); }
body.look-labs .ico { box-shadow: 2px 2px 0 #0E0418; border: 2px solid #0E0418; }
.ico svg { width: 21px; height: 21px; }
.ico.home { background: var(--seg); color: var(--fg); box-shadow: none; border: var(--bd2); }
.nav.on .ico.home { background: rgba(255,255,255,.2); color: inherit; }
.ico i { position: absolute; right: -3px; top: -3px; width: 11px; height: 11px; border-radius: 50%; border: 2px solid var(--bg);
  background: var(--accent); display: none; }
.busy .ico i { display: block; animation: pulse 1s infinite; }
.err .ico i { display: block; background: #ef4444; animation: none; }
.sidefoot { margin-top: auto; display: flex; flex-direction: column; gap: 8px; padding: 12px 4px 0; border-top: var(--bd2); }
.sidefoot .looks { justify-content: space-between; }
.sidefoot .looks span { display: none; }
.sidefoot .looks button { flex: 1; text-align: center; padding: 6px 4px; }
.pin { width: 100%; text-align: center; box-sizing: border-box; }
.ver { font-size: 10.5px; color: var(--muted); text-align: center; }
.main { position: relative; min-width: 0; min-height: 0; }
.view { position: absolute; inset: 0; display: none; }
.view.on { display: block; }
iframe.view { width: 100%; height: 100%; border: 0; background: transparent; }
#home { overflow: auto; padding: 34px 38px 40px; }
.hero h1 { margin: 0; font: var(--titlefont); font-size: 38px; letter-spacing: var(--titlels); background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; line-height: 1.1; }
.hero p { margin: 8px 0 0; color: var(--muted); font-size: 15px; max-width: 640px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 16px; margin-top: 28px; }
.tcard { background: var(--card); border: var(--bd); border-radius: var(--r); padding: 20px; box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 12px; backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur);
  cursor: pointer; transition: transform .15s; min-width: 0; }
.tcard:hover { transform: translateY(-3px); }
.tcard .top2 { display: flex; align-items: center; gap: 14px; }
.tcard .ico { width: 54px; height: 54px; border-radius: 15px; }
.tcard .ico svg { width: 28px; height: 28px; }
.tcard h3 { margin: 0; font: var(--titlefont); font-size: 22px; letter-spacing: var(--titlels); line-height: 1.1; }
.tcard .tg { font-size: 12.5px; color: var(--head); font-weight: 700; margin-top: 3px; }
.tcard p { margin: 0; color: var(--muted); font-size: 13.5px; line-height: 1.5; flex: 1; }
.tcard .st { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 7px; min-height: 1.4em; }
.tcard .st .d { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; flex: none; }
.tcard.busy .st .d { background: var(--accent); animation: pulse 1s infinite; }
.tcard.err .st .d { background: #ef4444; }
.tcard .go { align-self: flex-start; }
.tcard.soon { border-style: dashed; box-shadow: none; cursor: default; opacity: .8; }
.tcard.soon:hover { transform: none; }
.tcard.soon .ico { background: var(--seg); color: var(--muted); border: var(--bd2); box-shadow: none; }
.tips { margin-top: 26px; display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.tip { font-size: 13px; color: var(--muted); padding: 12px 14px; border: var(--bd2); border-radius: calc(var(--r) - 6px); background: var(--tile); }
.tip b { color: var(--fg); }
pre.err { white-space: pre-wrap; font-size: 11px; max-height: 120px; overflow: auto; color: #ef4444; margin: 0; }
@keyframes pulse { 50% { opacity: .35; } }
.intro { position: absolute; inset: 0; z-index: 5; display: grid; place-items: center; padding: 24px;
  background: color-mix(in srgb, var(--bg) 78%, transparent); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
.intro[hidden] { display: none; }
.icard { width: min(560px, 100%); background: var(--card); border: var(--bd); border-radius: var(--r); box-shadow: var(--shadow);
  padding: 26px 28px; display: flex; flex-direction: column; gap: 14px; backdrop-filter: var(--blur); }
body.look-labs .icard, body.look-studio .icard { background: var(--bg); }
.icard .top2 { display: flex; align-items: center; gap: 14px; }
.icard .ico { width: 58px; height: 58px; border-radius: 16px; }
.icard .ico svg { width: 30px; height: 30px; }
.icard h2 { margin: 0; font: var(--titlefont); font-size: 28px; letter-spacing: var(--titlels); line-height: 1.05; background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; text-transform: none; }
.icard .tg { font-size: 13px; color: var(--head); font-weight: 700; margin-top: 4px; }
.icard .what { margin: 0; font-size: 15px; line-height: 1.55; }
.icard ol { margin: 0; padding: 0; list-style: none; counter-reset: s; display: flex; flex-direction: column; gap: 9px; }
.icard li { counter-increment: s; display: grid; grid-template-columns: 26px 1fr; gap: 10px; font-size: 13.5px; color: var(--muted); line-height: 1.45; }
.icard li::before { content: counter(s); width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center;
  background: var(--accent-bg); color: var(--onaccent); font-weight: 800; font-size: 12px; }
.icard .row2 { display: flex; align-items: center; gap: 12px; margin-top: 4px; }
.icard .row2 label { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); cursor: pointer; margin-right: auto; }
.infob { display: none; margin: 10px 4px 0; }
.infob.on { display: block; }
@media (max-width: 800px) { body { grid-template-columns: 76px 1fr; } .nav { grid-template-columns: 38px; } .nav .txt, .brandbox div,
  .navlbl, .sidefoot .looks span, .ver { display: none; } .looks { flex-direction: column; } }
</style>
</head>
<body class="look-studio" data-look="studio">
<div class="bgfx"></div>
<aside class="side">
  <div class="brandbox"><img src="/logo" alt=""><div><div class="name">PlutoniCut</div><div class="tag">strumenti Plutonic per Resolve</div></div></div>
  <button class="nav" data-id="home"><span class="ico home"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/></svg></span>
    <span class="txt"><b>Home</b><small>tutti gli strumenti</small></span></button>
  <div class="navlbl">Strumenti</div>
  <div id="navTools"></div>
  <button class="btn ghost infob" id="bInfo" title="Rivedi la scheda iniziale di questo strumento">ⓘ Come si usa</button>
  <div class="sidefoot">
    <div class="looks" title="Aspetto di tutti gli strumenti"><span>Aspetto</span>
      <button data-look="studio">Studio</button><button data-look="labs">Labs</button><button data-look="notte">Notte</button></div>
    <button class="pin" id="pin" title="Tiene PlutoniCut davanti a Resolve anche quando clicchi nel Media Pool o nella timeline">📌 Sempre davanti</button>
    <button class="btn ghost" id="bQuit" title="Chiude PlutoniCut e tutti gli strumenti">Chiudi PlutoniCut</button>
    <div class="ver" id="ver"></div>
    <span id="status" hidden></span><pre id="log" hidden></pre>
  </div>
</aside>
<main class="main" id="main">
  <div class="intro" id="intro" hidden><div class="icard">
    <div class="top2" id="iTop"></div>
    <p class="what" id="iWhat"></p>
    <ol id="iSteps"></ol>
    <div class="row2"><label><input type="checkbox" id="iSkip"> Non mostrarla più</label>
      <button class="btn primary" id="iGo">Inizia →</button></div>
  </div></div>
  <section class="view on" id="home">
    <div class="hero"><h1>I tuoi strumenti</h1>
      <p>Tutti gli strumenti Plutonic in un'unica finestra. Scegline uno a sinistra o qui sotto: passando
        dall'uno all'altro ognuno resta esattamente com'era.</p></div>
    <div class="cards" id="cards"></div>
    <div class="tips">
      <div class="tip"><b>📌 Sempre davanti</b> — PlutoniCut resta sopra Resolve anche quando lavori nel Media Pool o in timeline.</div>
      <div class="tip"><b>Un solo aspetto</b> — Studio, Labs o Notte vale per tutti gli strumenti, editor dei marker compreso.</div>
      <div class="tip"><b>Nuovi strumenti</b> — arriveranno qui, nello stesso posto: basta aggiornare PlutoniCut.</div>
    </div>
  </section>
</main>
<div id="off" class="overlay" hidden><div class="card"><h2>PlutoniCut è chiuso</h2>
  <p>Per riaprirlo, in DaVinci Resolve: <b>Workspace ▸ Scripts ▸ Utility ▸ PlutoniCut</b>.</p></div></div>
<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let ST = null, inited = false, lastLog = null, fails = 0, lastLocal = 0;
const post = (p, d) => fetch(p, {method: "POST", body: JSON.stringify(d)}).catch(() => {});
const full = () => Object.assign({look: document.body.dataset.look, ontop: $("pin").classList.contains("on")}, collect());
function push(extra) { lastLocal = Date.now(); post("/api/set", Object.assign(full(), extra || {})); }
function cmd(c, arg, quiet) {
  if (ST && ST.busy && !quiet) return;
  lastLocal = Date.now();
  post("/api/cmd", {cmd: c, arg: arg, set: full()});
  if (ST && !quiet) { ST.busy = c; renderCommon(); render(); }
}
function setLook(l) {
  document.body.className = "look-" + l;
  document.body.dataset.look = l;
  for (const b of document.querySelectorAll(".looks button")) b.classList.toggle("on", b.dataset.look === l);
}
function favicon() {                          // logo Plutonic quadrato: icona della finestra e della barra
  const im = new Image();
  im.onload = () => {
    const c = document.createElement("canvas"), g = c.getContext("2d");
    c.width = c.height = 128;
    const s = Math.min(128 / im.width, 128 / im.height);
    g.drawImage(im, (128 - im.width * s) / 2, (128 - im.height * s) / 2, im.width * s, im.height * s);
    $("fav").href = c.toDataURL("image/png");
  };
  im.src = "/logo";
}
function renderCommon() {
  if (ST.log !== lastLog) { const L = $("log"); L.textContent = ST.log; L.scrollTop = 0; lastLog = ST.log; }
  $("status").textContent = ST.busy ? (BUSY[ST.busy] || "Lavoro…") : ST.status;
  document.body.classList.toggle("busy", !!ST.busy);
  for (const [c, id] of Object.entries(BTN)) {
    $(id).disabled = !!ST.busy && c !== ST.busy;
    $(id).classList.toggle("working", ST.busy === c);
  }
}
async function poll() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    ST = await r.json();
    fails = 0; $("off").hidden = true;
    if (!inited) { inited = true; setLook(ST.set.look || "studio"); $("pin").classList.toggle("on", ST.set.ontop !== false); init(); }
    renderCommon();
    render();
    if (ST.quit) { $("off").hidden = false; setTimeout(() => window.close(), 400); return; }
  } catch (e) { if (++fails > 3) $("off").hidden = false; }
  setTimeout(poll, ST && ST.busy ? 300 : 600);
}
for (const b of document.querySelectorAll(".looks button")) b.onclick = () => { setLook(b.dataset.look); push(); };
// Dentro PlutoniCut (la casa degli strumenti): niente intestazione propria, l'aspetto arriva dalla casa
if (new URLSearchParams(location.search).has("embed")) document.documentElement.classList.add("embed");
window.addEventListener("message", e => { if (e.data && e.data.look) setLook(e.data.look); });
$("pin").onclick = () => { $("pin").classList.toggle("on"); push(); };
function start() {
  for (const [c, id] of Object.entries(BTN)) if (!$(id).onclick) $(id).onclick = () => cmd(c);
  favicon();
  poll();
}


const BUSY = {quit: "Chiudo…"};
const BTN = {quit: "bQuit"};
const ICONS = {
  beatonic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M3 10v4M7 6v12M11 9v6M15 4v16M19 8v8"/></svg>',
  plutonicedit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"/></svg>',
  subfinder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21M7.5 9h6M7.5 12h4"/></svg>',
  nosilence: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M3 12h2M7 8v8M11 5v14M15 12h6"/><path d="M15.5 9.5l5 5M20.5 9.5l-5 5"/></svg>',
  plutonicamera: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="13" height="10" rx="2"/><path d="M15 10.5l6-3.5v10l-6-3.5"/><path d="M6 4h4M8 2v4"/></svg>',
  zoomtonic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg>',
  soon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>'};
const collect = () => ({tool: cur});
let cur = "home", frames = {}, built = false;
const icon = (t, cls) => "<span class='ico " + (cls || "") + "' style='background:" + t.color + "'>" + (ICONS[t.id] || ICONS.soon) + "<i></i></span>";

function build() {                            // elenco a sinistra e schede della Home
  built = true;
  $("navTools").innerHTML = ST.tools.map(t => "<button class='nav' data-id='" + t.id + "'>" + icon(t) +
    "<span class='txt'><b>" + esc(t.name) + "</b><small data-st='" + t.id + "'>" + esc(t.tag) + "</small></span></button>").join("");
  $("cards").innerHTML = ST.tools.map(t => "<div class='tcard' data-id='" + t.id + "'><div class='top2'>" + icon(t) +
    "<div><h3>" + esc(t.name) + "</h3><div class='tg'>" + esc(t.tag) + "</div></div></div><p>" + esc(t.desc) + "</p>" +
    "<div class='st'><span class='d'></span><span data-cst='" + t.id + "'></span></div>" +
    (t.error ? "<pre class='err'>" + esc(t.error) + "</pre>" : "") +
    "<button class='btn primary go'>Apri " + esc(t.name) + " →</button></div>").join("") +
    "<div class='tcard soon'><div class='top2'>" + icon({id: "soon", color: ""}) + "<div><h3>Prossimamente</h3>" +
    "<div class='tg'>nuovi strumenti</div></div></div><p>Qui arriveranno i prossimi strumenti e le nuove funzioni Plutonic, " +
    "tutti nello stesso posto.</p></div>";
  for (const el of document.querySelectorAll(".nav[data-id], .tcard[data-id]")) el.onclick = () => open(el.dataset.id);
}
function open(id) {
  const t = ST.tools.find(x => x.id === id);
  if (id !== "home" && (!t || !t.port)) id = "home";
  cur = id;
  for (const el of document.querySelectorAll(".nav")) el.classList.toggle("on", el.dataset.id === id);
  if (id !== "home" && !frames[id]) {          // ogni strumento si carica la prima volta che lo apri
    const f = document.createElement("iframe");
    f.className = "view"; f.src = "http://127.0.0.1:" + t.port + "/?embed=1"; f.title = t.name;
    f.onload = () => f.contentWindow.postMessage({look: document.body.dataset.look}, "*");
    $("main").appendChild(f); frames[id] = f;
  }
  for (const el of document.querySelectorAll(".view")) el.classList.remove("on");
  $("bInfo").classList.toggle("on", id !== "home");
  if (id !== "home" && !shown.has(id) && !(ST.set.seen || []).includes(id)) showIntro(t); else $("intro").hidden = true;
  (id === "home" ? $("home") : frames[id]).classList.add("on");
  document.title = "PlutoniCut";            // (titolo fisso: serve al "sempre davanti")
  push();
}
let shown = new Set();
function showIntro(t) {                       // scheda iniziale: cosa fa lo strumento, in poche righe
  shown.add(t.id);
  $("iTop").innerHTML = icon(t) + "<div><h2>" + esc(t.name) + "</h2><div class='tg'>" + esc(t.tag) + "</div></div>";
  $("iWhat").textContent = t.intro;
  $("iSteps").innerHTML = t.steps.map(x => "<li><span>" + esc(x) + "</span></li>").join("");
  $("iSkip").checked = (ST.set.seen || []).includes(t.id);
  $("intro").hidden = false;
  $("iGo").focus();
}
$("iGo").onclick = () => {
  const seen = new Set(ST.set.seen || []);
  if ($("iSkip").checked) seen.add(cur); else seen.delete(cur);
  ST.set.seen = [...seen];
  push({seen: ST.set.seen});
  $("intro").hidden = true;
};
$("bInfo").onclick = () => { const t = ST.tools.find(x => x.id === cur); if (t) showIntro(t); };
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("intro").hidden) $("iGo").click(); });
function init() {
  build();
  open(ST.set.tool || "home");
}
function render() {
  $("ver").textContent = "PlutoniCut " + ST.version;
  for (const t of ST.tools) {
    const busy = !!t.busy, txt = t.error ? "non si è caricato" : (busy ? "al lavoro…" : (t.status || "pronto"));
    for (const el of document.querySelectorAll("[data-id='" + t.id + "']")) {
      el.classList.toggle("busy", busy); el.classList.toggle("err", !!t.error);
    }
    const s = document.querySelector("[data-st='" + t.id + "']");
    if (s) s.textContent = busy || t.error ? txt : t.tag;
    const c = document.querySelector("[data-cst='" + t.id + "']");
    if (c) c.textContent = txt;
  }
}
// l'aspetto scelto qui vale anche dentro gli strumenti
for (const b of document.querySelectorAll(".looks button")) b.addEventListener("click", () => {
  for (const f of Object.values(frames)) try { f.contentWindow.postMessage({look: b.dataset.look}, "*"); } catch (e) {}
});
start();
</script>
</body>
</html>
"""


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

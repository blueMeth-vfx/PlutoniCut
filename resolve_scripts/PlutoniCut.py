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
        "id": "zoomtonic",
        "name": "ZoomTonic",
        "tag": "zoom dinamici",
        "color": "linear-gradient(135deg, #22c55e, #14b8a6)",
        "desc": "Zoom automatici sulle clip di una traccia video: su tagli, inizi di frase ed enfasi della voce, con ritmo, intensità, punto d'ancoraggio e stile (scatto, morbido, rapido).",
        "intro": "Aggiunge zoom dinamici alle clip di una traccia video, dove vuoi tu e come vuoi tu: solo il video cambia, l'audio resta com'è.",
        "steps": [
            "Apri la timeline e scegli la traccia video su cui mettere gli zoom.",
            "Scegli il ritmo, quando zoomare (tagli, inizio frase, enfasi) e quanto (min e max).",
            "Clicca sull'immagine il punto da tenere al centro e scegli gli stili di zoom.",
            "Premi «Applica gli zoom»: puoi toglierli in ogni momento con «Togli gli zoom»."
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
        "8e3L1sH2y/2MQNjPhh58i8aaj5/nFKDd2Mr2tjNm2fXtqi92HxGb+b9moZrsbWH5xV19cLCxv/Xsll1tN5eOkT8e729ttrzN"
        "Le4vbaK8w03fpU2+gYFtFAZ387bGdZima+kx683bkq51oQpbW+f4tby9GoCzleSWjsWrrNYBosL3p2Ke+41J8rs0Ntsi6EBQ"
        "kUm7OvpzSP1zg0i6QZJaGIBKYMuoy9t3x8cHLMQ8LiJDOaQk1CQ9CLuXQ6nnsC2IF/PzGJnPl9EZbaogaYnU7BmpWfblrTLQ"
        "70ld2r+OulQIX/elMM1ejn3VNObxI9K+jW1XnmluW3mmvfm43nq0Vcf/N7Yo0OhbMA7wrCctYarexlYmWT1+VG9t1yFugdFv"
        "B8qZXLL1eKsoXS3dCJCpHrdeisz2bavf7G48Mnuhvd98trkv2oYrTQ1gcVFKY8ld70l3oz9wyJ3uLZfcDdr9nuXQrjTVrG+T"
        "NzYet393QcqBZ6t9oxz1aFGOysHe2r5BinL6aG/fTYhqLcpQmyLmlmQoogB5zmb2obTkZQmruShh/SZavNHMabH3qEAdDQ3O"
        "+lEU2ilJX01H9nIAeqvklZPo8oi2bh5RtTDWFM27cpMFRbqu5EzU4E4nHIBuCW3jkW2ZZ6224yjHoNi8aBEEFxQIX9e2qJVP"
        "VUXXh64khBThxerum+0HHGyuJa/B63YeLUKhDDbgF/GqJJtgzasNYUtW+c6tlgbJa+khCXA9N9u0YzhsCZFk4DDCr/wtKgQB"
        "DMI0k/7sP2qj18ApvaZG644Bjazbp6h7Gc/WRmF6uQwYzuiw6Ly3iOQNVpH711lZsDrwAucZDLllk0IDxxnAgI2hB9aQYYSJ"
        "I3jofLym+WE84n803fHUwLNJwCCwoi92nVamfYjhB9bHtU9xHzeAgMuRB+zk5h2JwyHj/pYojD4zbKGk6ZlqWgjXcNCmZWwr"
        "ivLGfDOly7EhvjNrFdJSGZcMiKwzGm1sMmy3BO6QqLLfmI0auIvloAbTWUA2ia5nbuXCa2OEclZkR5Yrt2iBhrtzgOcGUyjY"
        "roTZYHychkHHloi0i1MCuyA3R3N8xV0sa4cK8pJ7PV1cWFnD4hLKk7HPEa3mWPInT57I2wUwpYLpWjozffXbgdMnhkOZUVEo"
        "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"),
    "plutonicamera": (
        "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"),
    "zoomtonic": (
        "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"),
    "beatonic": (
        "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"
        "YCI/UkGC+ZsLLRAXe4Y3mxLY8nMlOmauI7VP5W09euCaMtf28nbYBmVXmP/COq6+vfSVOw9u03tthplIjf7FaZ/xGJNpvRJd"
        "uHP6RySf5U/hnOEsbsBGatOvJp1N24VP71vu1NP7lo4miU1dKJnz3Wq006osN97Z4BVmhckl3E2p3m5m6Fb12dP7uGTJbazN"
        "oN/hnbi7Unk6vDiTjDXJUmOSpctYA5kj+01vihtlTApajS5JSmiUGMf3xd/jnKw+C8hWEQDmxeV8CFw8VLR5i3Mzelr7nL5Z"
        "w1eq0Zfm0LHvoW5p8p38IQfPbtWAIfsO4qzP13oMhF7nCsszRJcchIHCGv2gIO84hiQJNQXRrD77//73//Q/o3ozS+G4Jp7e"
        "187kOy2hH743exZ8rIAEvksorjUeS01R1h3KI/5ksgZolgEzz6wJkAk/6Rc2DyX0cCC/fedK7iTvrT57hZ+r7hJGW332mr+S"
        "oRpx3VfqYvJkZ+hXhHSuBDRX6c19QQOAG9V565mMzFNT9dmmjQ5YpJCFEDaMd+I2vT+miP6Zrjhzb59Z5F/S+ytQgyzZ09Nn"
        "Ud25aDUxGryfqs6zqFg3YeXnQ1ZXkbo/lEfQJ7krHn7Z+3z2b+6dUUpx9M5Q8tQosF7JVTwtf6/fBDYjnAUpQPfFBBhLqhg8"
        "eyohEs8OraJeDmc6KY2HYWgbo9eym6WL9/X50inXanfPcvs1XR+JowgLk/smZSJo67kvO1vkIVf61vG3pKhm8Xv47fUOPM8R"
        "4LWo+lXo3U16ofNT0gWpAWsEh790osJbynrD+14CiLFsoqI/U0YGAUyfPn1nGduOf/iy8obrmSSvh7qw/QLH+uu//GsJsme6"
        "k22P/oL92nL71XBYfeHj8m2LgzshCkenZVXJHA36sN64IRarevZUJS7OFz9XmOGNXl3iqMUnSPe71U0cbMAf4B/a92dz6/ES"
        "AnE9+v34dD59UuyDhh5ZL2Dk8AuUQDgS9FKOkkWxlFr1WW7R1Upi2eMs44WwYbZo1zFMdHNY4D7+YPA0nfAdLZkY8Zu4T6E3"
        "gcfYrNjGL6fWpaN/rpiVe6gqw4O9sm/AaWSsxISISkIJVG9X6ntM9MPocnxmxQnLSoYU+HvZvCGN/2bTVjJlK6uZRDOYH8mq"
        "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"
        "LjD745sdg2trytrX/n/517mL"),
    "plutonicedit": (
        "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"
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

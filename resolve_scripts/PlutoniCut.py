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
        "eNrNvUlzI0m2Lrbnr4hC3WwClSASBMkcwGT2y8qhKt/NgZ1kDS02jRYAAmQUAQQaATCZlZ1mb6WV7G0kk8y00V1Iz2R2tZC9"
        "jYa76/4n/Qv0E/R957h7uAcCHKqqn1T3NjMQ4bMfP/M5XqvV1vZHi3k2SZ/F42QWRxvRdLQ4TSfRNJlFz+Pv00k/jd4neTa6"
        "SKL6D9nsPJ/G/SR6Eh30Z+l0nuPpu3k6Sucf8RS01Vh7GUfTeDZPokHqPi3mzWgUR/04j6NBcjpKo3w+W4yTydyV6bfW1tBE"
        "L0UpGVSM1+N4nmIoHBb6nSV5Eo0XI7zTIhhx/nHSj+pJ3ooWk/V0Mk9mF2k+jxvdKO+zo2Ttz4t4MsjYai+NZ5jPNF7kSTPq"
        "n6Uc6ChuRslkGOdpM5rHHFqSowUOrRElEWqPEjuk+nSWZrN0Hq8318ZxyimhZM5JLZLoIhvprIcpGx0sZvE8jsbpJB3HaGkc"
        "5zmfBslohJGi4QEKoBC7GSdz1E1Ha7M0xwTjeRblC0wpjuazuN9P4+giHSRZ9Pa7d98/5bz7aKoVvV6PF4M0iyYZViKN5lm/"
        "H2MdD5LRcKOfTeZxOkkG3eg0xeDxGx1E+x/nZ3ziUk0yDtds9G6UY/GSaDgcT5NTWfM472NOXLSRdtRaqwF41tLxNJvNo5/y"
        "bGKfsVNn9jnL7dOfF8kisT9m3Iex/ZWfLQBB7teiN51lfayle/PRPc6T8RRr6hqan82SeJBOTt2LdJysrc1nH7trEf4bzrJx"
        "dDafT1syo1lkin0d58m3h4f77xOMK59/i/GMklkzOrTt8eOBVFlLLvvJdB69kpovZrNs1o0q//vSrujjaKv14Bf173XrqudZ"
        "/zyZh/XdON+kl68ma1K4PwJYVc2gHpb2e2noMvG/QZyMs8mJrmge7UWHs4W/lDODBDBNwDGAlCBtTlTOAzAhbA7iqAJJ2DV8"
        "i8OjKyhNmskYNGOAT6sA4KKTwcUs6HpPXrVyKRFPp/WaqVNrrK093d9HgVqAggCgX0Ybv9l/aOwljhEQFnCSQW31Z2fYpOTe"
        "i8FpwtM4zgbxiHghqmGEtWaUTaeLGQ901JtlH7CNEbDXIBliEedZA01yZ1GBS7cA3k37OGlZZPb7+Yu3h+/fsbbi5WZ08O71"
        "O2IEARwgs2w8XeAAt9DSN8SmunwWJ/O842xjYGxbz1VWDH640PKNJtpLJ0ABrie0N11k6zhyk0THny/QXdofZV2iu6FdiTFA"
        "AAiYI8ETcKdrIhIUmpwuktZvuw8nr9998w67neWtKbBN66csndTxI5lcpLNs0jpN5vUaAOL508On2IJaDfOrfT2K++fj+DTt"
        "R8+B2E8nteZa5TmulegeWzhYTAmrfHy5yNNsIi8Vuvlo1ntlk1ND2k5G2WnWmk5OAbInb9/98OotptG+bD9sy39ROuSkJoDe"
        "aA/QPJnXomQEWtcW/IJzB2RN/I2B4d8zoOOYOPo0jddO9l/fdkksweVgDr998ebFyctXr1+UW0HDKDs/S8ZJa345rzUMtsMw"
        "BilobCYE5eskPmRbaycHLw5Xt4MhtEbZB2ClRnQ3qrVIOdD9Dy++Lhe3uJ5D5/MgRR1/0NJYY+31u3f/fIDK9Vo+J2Xi7EZx"
        "T7ZlkqEqscPB4dNDDuhTDRtQ62L++AruYL7I+WsfSzTPWnzZW+QfbQGciTmeX8Yjsgm1KVBoMsCLdqv9ee3Zm+d/QItC2Fp/"
        "4N96I3JLI2cBZ3rkHZWmnoZ0Lh/sIVngfEoxcg3x2sGL99+/eC9DzWcX6OxtNpHOCX766/Pa2howSDTK4sGJ7EvdIHKHrfnf"
        "h3R+BvSTYOWLzUVLM+wg0OuwG8DqHF0OW0T+9UYLo02n9YYrMEvAnkxQBvA5JzZZWuyGwql9LTUN1n8h/+DMdMvtudI6nzy+"
        "SMx8eAKq5gQIGcfnCWAhV3hKLsGinWTneyRXjWvn/qFy7sPWB/Byptdrhj4FlTXjPZEB5wBPAEZe/43G644Pj8d8TBqCYZM1"
        "7Wck4nu1xXy48bByIjxNrcFiPK2jlWY0bPgDmSVToMCksgf37jbTB4E7+QCqAbJWn6fzkd0xcIaGTgoT7FNIoIu0B1QpKANH"
        "ZwSObU56V8/59wdtrdEic+mxCP35x2mSFzyR/rZfMYbi+wJwrJ9bHNto1AKPP9vqaOVsMRmgwNGx8kycRr9XP8MCjDxeaIIi"
        "i9Y3yVzHc5hczl8nk9P52Q/1s2JJcRYmEY45ir7KteT3nN4oQalwX3rFoPo4YvPkZAEMlg2Sk95iOAQynGAvNhtBndIAfuAo"
        "e82ooiQG0mtdxKOFEA3Zie4SHepfN4T7242lSjKGZ2QsybrJEPrNqKokxtDXMQB5gLnICdH1mu79yQ8pNp+TqTW6lRRSNqYF"
        "gEomA3+NDaIQXlQH9GKyGBswqZv5gIq+/O7ts8M/7r+oW1hoff3uHY6b+/ntD2+fez9f7z99//RNo97vgaS0G2teXzIUC+JA"
        "+SGI5+DowIUNSTD2hDAUQH8I8gyKnFiEn1DCs2j+4MWb/fcvoudPv3/69vBVVAe/fJZYBro/AtsHKRTlDNPRaEmrh5TjIjkb"
        "frusSkmPjGIG+gH6vx7V+1mersv7GcQgSmwxxLVsBqyRNHbt6cG4uZxsj6eH4uB4ASE0scIkYIyCqdbjAV7XmaOH0Tpl7jFH"
        "NIW4WhzTgm35QtmWJVzfXkaOy4f7Jgf82kOucHJgD88+RhbPTrVBHP1VIKE/7RE5wevSr2rWrnTKVlcvfoHTnh8X8x3Fp7ly"
        "ge32ZvQXfeiYh822L2AaBjCfZjhkojRp6n4vejH57uEi62cBGmsX/WQzxQ3cZ93REhVEgTN+XInYA34hm6L5XpaN6h6mep1N"
        "TgVNbHTajeh3mMLDhoz8h4OTFz+eHL7bf/Pu4LAKe2SKS+sCoRGGAt6NnaxAGOEGs8dgI+sbmzzY3v/LMi/jrWRENI6uTP/S"
        "fbck15uDIVoY4dJ4InFOplmKurMxFEP/0EHeqK3OL5swlrj7D+0QBGsv2iyjg8nNOMS2RcS9GVifE9kdBUnFwx/idL63CX68"
        "wMJPSUTiQEQFPEN1JYpHRXmCvogB41EJTddVVFY1SIHgOIbZYlL3oDERToIKpxb/iDzD4RQ83Rmkl6DAY1bqlml3QGTM5Bo+"
        "mRFWcXmTdIlCJp6d5aMkmdbbrY7uhdORtVQPVAd5BjHew3SaRuujPSjhBtdfsHejjxBYs/P6oFhe6sFQ8GfsVqqkI+2CE52C"
        "C84iCgAQ1bik8UUM9FTzScRApVC2CO4VC62iG6FwcKSvj0k/wIvan8WsvZdYdlfeffeEB5VI2GNQac+IKqGk0rCjq1EEnNY4"
        "roGMSXAbOtL3xw0zNJ2EvmxGpb2RDk0Fix69Jly5YM8PXx2+fnHQDCq7XRAh70oZo0J2ECnvZiKD2WbXg4gQ7LQ+BBByVQ2b"
        "c3PBAG2dqGxdH5sBi/x9ZCVuLs3YKwyZvFySYrotpprNE6MlrVcrTwsIVdWmU5Rtdh60gCBam6K2mkIBBOxQv4fJkXsSBXv9"
        "XjxN7/E5oQre6bIEeN35x5BOxtTxnyb1HHr1ZvRV3CgvgC2MBQUnq8UGUJUa8r9HrSCYvZiLd0+UH/gCLnyv0257jbFii02c"
        "QPk5hb4nqbNUo6LAGc40VqX2DJADjmDjEN3UTHc3Ka+SDWoAVdZHgCIOt9G4smoMBnSDDcyykepZNqCMnCW1Uq2iUl4vffog"
        "uh2Vu6XHYu0G2ck3Lw5l8fyjpYBhlDDHIe4tdgHvpX1RJOVYaxzW39caR+0CVQSnx5z+qeCHe7VlNCut6XbuP/3mxcm3h29e"
        "t+RkAdOYc0X9GGS0e2fz8WgX3Hk8A1jvmY9rS+TXdOaA7upenVAPFYIUrzca5QFUdgJMhnU8WG48W1Dbw29H0+N6pTAHQXaC"
        "zibQF2S02c0XU7KBZYtHvfdxDsI6T6cZPlrj25hq1kk1T+RNDA1jW5ocztHmcRWrkifd6xrxVgft3Hhh6rV7VMUSeO8N4wtI"
        "wpMW/lTJpx5+pd6ZuLVXiUorRmc1aqgEBvI0uaeq3+tn6bXRoxpTxdKaBTRoclKiju22J4sb5Fz/epadJ5P9dKrGlmaEkzpJ"
        "+kQ5kC6T+dLbpz1wRclA3pcWQLBaqQOH/bkGpbEvna3SZIhkksaq82OntdNulxZpJd2pRr9AIfsQNH4ZDqHcBFZXKrcM+lKq"
        "X8KZDcop3kiXJk8O0RFUMHfS4kwwn8DFpKFaJOFIerVPn2uN1iDxl4bQpno77U3KrN1sWdj7p8+3Qo0l5JTMa1dzCwNvLCXM"
        "1h8Prqusi8peZG6fPoebTg17C9atui3JJgEr5hfY2JpPp5YPkhEifslZCuqAfwwLW+5MOOYTtdVZ3gxae1pPK+yw9ZrjRWrU"
        "OjUdT9OwNVvVZtir+HjWkhGcQIBPLmhGruTqlT2lcaG1mA5IRlCTtZsiFBXtzE7iwQBsR+6QsllHrX2kpohjy6AuZqM6IWqv"
        "ZpGn1e7T8N29d69gwO4M7t0B/30nqodNYQRowLG8xLWGOa6jdXAm6c/LumXwcgtaWqE6TiY/Q6YDTzKAWixZj3rxjM4U1DGr"
        "AjLKIqqf6dhAe2UfNtGmZ5KVlj2zbCt6hbrqoOGJkYl5Ic1ja0hAnNMKNBdKRgqh0Wp2jmobGwD9vRokRJkPfk+SDxs6xZr+"
        "zjaGMBLMNyCWuTcYTwyF24YZ5gZm2D8v2xhRVBva4DLt3Rk07wy4xvwlDVE1tkEWawNmCBlEYGeD6Y3mpVkGtJSdmK5qDcUJ"
        "WCmBwqPjsp5vr6zno/qoB9ZcqGvZ8ggD2+ksHr8E5st5hK/6Xr98eL+BQtX6tnLN1++ePX1tDZuNxhJjxyFVaMNlXlBMHAVr"
        "wcJYjG+y7HQkdl8FHz49Lfh2/uzLh1ZymawcqR3uUvNv0v4sy7OhmJMJlxXtj/MEH7R9XXvBr/CFaQEFzbHYY9mCQTzD7nvb"
        "UMysds9rM7+nkzIHgqr2e4aa5ffexP13B2GBSkt2qUU3DzlcVU2GJWp2Iv6OFONVP6AW1Cb9M1JGAtREebVTGdpG3+2GPKWL"
        "sSyV7WSDS2aXi7X7orljB12fwvVFvLc7IxY52BIaN2FknHNSa184wqP+Mc6THHQKUEDPe16Z5y++f/vd69c30ByjKrjXqrr8"
        "lMxmv6xZsfRgr2SAe+pw0FihSCoozS2ZLk+V/yHp+SjVNFy8bcmiAQdaXA9SB4cSSLfJxR5N26ESQOzvx84Xac1o4chH1BW9"
        "paCee2ohR2FQw3rD0wReXKSiFley1oQekA5sPkYfGAeVQubnR+DZ9bDcurQZwxVuHndF7wufJ1hsFgOqusRA04rWOZp19bHL"
        "TqEe60S5tfBQS99fx1ejwkelIXSSapUpOge8rstEYGyh9yIc9yYJ3A9ZHd5/YEdo7qHX4IDHMFML6yi+yGaZozu+0Vmwe4XR"
        "WbZrr8TAGDpI3rd2J+8W84capYqYCykH7lUOwrBiAfkW7qBhKLh+9jW5qvkSIbBas2YYI44yITsLTdD8hMtcwbqrvpX8XQA+"
        "3dXsOfhJAMXsFI0Jr8n+2SCPIvSnO0uMtjp7vBhP56tbUmjk3wDnjAfcXHRTksfNWMXthKBeq3nFBRRUOsCb6/ETmz9CyeM6"
        "xnK1BKWym5Xj6cPqrIu9Ea2Oc3wdrSeUCWG3j2kUJMezrCwwnpfwTk16cLGqkP5Uw1d78f79O5hA6Q7LI7B+J1/v/mliOEJZ"
        "QddIiwQunp9g1PXG0cYm/KO6x41GZdtG1VgT8RWD1gaTsDAmByebiiVbXv9QovH19xse/D2JOq122NxVwOmBwnU65Ct1xFJW"
        "tkwNyL5VOFbLh2Cp5HpgoZZnMFqhYeGXeuOXkgO7GliIssgNsmyOs388llcZC1zv7Mh6lVqQ4wCIKPtYgNadryKEMJ2CctJ+"
        "5xmFiMyt22W2ruhurWRG2YRpaVWTPoUYig86/dToXnkBUQIbUiOFyI03VLAHVv6hi9dxi5wPdnlSv5mS/Tf2aT3EbEcpkZa6"
        "kCdwH4ArAdzyaeRJ6FcAKw/AC0jtACgWOsFm9F9l2Vj8/Zplr//f1tPz/dPDV+/ePn198nKfTn6fOlutRw/ud6PONt0l70Wb"
        "MJE3o84jvO1GW23/5faD1qOdTjfafui9XQb2HdTd7kb3g7qbm49aDx92YTzwXlu/O5p8T4ZQOOJ/BgiH9J6DkmcuCi6+t16O"
        "zu2qhrfqeWnd6xqOUcXJnWWXJNRxX/zrgnm3oCMf577tEacClqz6EGdFq9K2CLK8Wan/kFYDFxczEUDv4ES9/G3rTuCb1Z53"
        "/6Tf/tRLJ+ZRBJJmNKs9W/2xpr9qvsgYiGwvX77Zf/GNr2qVXlvQOONI1tu+eHhkSx83rmTrlzCcxy/TcHvUpywMLkecd8Fv"
        "9OMp1iI5Aa2Hikk4o1/IwVcx2c3I8hGdkibTbEH/hjo8xm6kE8OYm7rCW6yt6cIQ8PxtNFvbX8xmYDRPMJGfoOSth6oZ41FP"
        "v419LfAmnkA9TXilx5dW3rd1S23ODcKwjU5/ojtOuUO/v+lPXrOHrrqoG39ShG4mxX7mo5Pz5GN9Pqqydlrl0p38L4ZzmI/Y"
        "+HeTFNzZK9G16xu6rNlDdq2jgV9FCNLImIlrxaB45K8clEMM2tqBKjrrNbtgL6HiSN7T4HPTYXW2W9YFYsjK+ck8O5n36/ID"
        "/gEFAkppLI0v60Be5N9nVHEKIrJoxii1zQfWbwR7VLvT7gy64R9Z32F07x7+ptFX0RawJNbXe3Mf5PEO/pqXeGd/3cEPCznz"
        "PsetM6jP+/7AIbSsf/tt982b7sFB9+VLyD1ZtL67DjbgCQ77XNRCcL0B4ctIZyjuzB3BItBQp0fxCM6H6aAQgkTbDVw87zt8"
        "W6DiXaKpbs1701x607JvjKa8W7gK0Eo6FX+A7aUdc4x/ACDwnhkDvTRlG+rch0tVcFwSl00DaPieXpRemExV49dut6lQr5/J"
        "JkFJMW7Yp5xPaOGuIwTku8/hpTbMCgCnQyMjzhITcJYY9kB1oMUeUMs6oY4V24BAvplsERwap4U8KubGTzVpBz7sR0C/NWlM"
        "fny2yyoeX6OlSaO6Q/3nQHWqEtLGXEONUBEpkjTEDJjqsULmPHI658+wUvM62zHWG/GnvYHuR7WekdeUYAu2hD1Q40ztlgyz"
        "abNUT8i96+oVfr2G/P5qIp0GHR4dXz/ubGIdUUx7ubTzYhLDU3ngmrstry/NLqmMaMpli8fWl/dTLcUmp/QRwFzxKFPmWt3J"
        "VU8tI4AKAFaJaSIhHintgdBpyjmTxeCLjG+yyecAvgkb1o9ETJmYJOwfBQjvawAqVDjw/clmMQXE+QLRa5EHxDAPDOJ7sCTw"
        "hfhZtKL3sAPM6CkC140eFjtSSdgBdQwk12u7XTqgTCP4vSBALyYD88aHcGO9EuD0wdbMSNttWpjAkZY+LPHRvpviUR5gVdMs"
        "vWdtfA/RQbOy1Dzzy9jRQYTJFaVSlW9/rB7hoVmtSBRXKaQFLBMlIg8fR3UPucM04o+f+AuPMUnKmOpxtutG04se70Xx6t5f"
        "q/QFfITQwItkklIXlk1VO8bBmM5sPfGeF3L+6yUURLZdAA1mihLXXrz9ngw7A4wgJ1RIioVDeIwS0ThfO2GdZ0+ffftCjbTG"
        "9EW8eQLWNxlBiyZmtabV0Ukc8YlHNl+bMdBh7WuS41IfDe4LfRkRXKyEk4ZnRdiGYbfADHaLdEn7U1riet1q2FfSP1+4PWI9"
        "SituMkvbVXw6QmFFV5Mq2iWNgy7ZtbSETIkHVEBtyDfHKDDxD5PhgY27cEmlL0bFleQkZODILZQkBm1bxAaeFjn/YprLRY67"
        "09oaEn9xJhia2yfiqo25X0LX7UptfW0jrakVVPpTE2DMGLfapj5L15Rk5edQ4tc276uVagMCzZUSyq1knWqB5qr2razDpSDz"
        "Z/ey41GVWfwBa4zoXbF03Iz7tQV/QM2H7egm/30ZibT/7c8qs68MOgCrMv1IHehk6j7RpjuZtohCTUwMBn3UJRXCQ4P8bYez"
        "AvsyUGe+x2kHDCK0VfhVZ1WqAPzwmDFBHagNrrZsJpZGfghiiMYlmV1IP9rK/wxZuB4fdcfo84djMKb5GXRWdfCRP4CB4+q2"
        "xkmMRgHte5sNNB1tdR7cf9hqh14gRLWdNk/OFEqJ0802R4qtSseLcR3dQduRbOyUGAAS8u6YCtEj1Wv4/Oqgd+xvoB8aX15n"
        "mso/ll8yRYBEeI+DtZeyLflbr515bi6x7glczfJVWxIsKa2kLI0QFjiC0kzaS09LziixlMg/xH6cY4l1rNi5EqcI4xEHfkRR"
        "6IduPRVmUvarYlNPxrqpOdb9EoUuixVFO9y/H1ZtIrcjPbYbiYZ0I3ngyjtYwrgMq13Mq3kmZfsduSHrxIgSJZVTmuSBF3y7"
        "nEd1jBhgclKoaIBAoXWxExiSc6S8kyeyVZGpwl+iitexzlj1HpVdXB8W8+gEl1yhcXJRSSkMN32UznV/NYC1mrk20gTXwTDY"
        "og1I54afg3LgCWCV9mN9d6DeNVC/9Qq783nT9JKIRERfGxlFM5A0qPwwa1zyuTNvwaeZ5X16B7k7yBtEdwZcTsM5c7POmx6b"
        "7B3i8ZQiog7yDWyB8T5EAE7Xd5blOdxjUY3zS6dQ+VAtjvNHV4xoPxYvN2IpNGcVIn4AJKguW7lCaSXHBKpNNxqzZE1/VV3J"
        "S5CPy03DIKBabHjDhJC05jmesqFsMesn0tyhmg4GCINLR9HcMaYCpJqzRSOlJAQkT1WzTVAlT+S5fc36bZHQdaRFB4aRNyLj"
        "PdkMgVMsjMdF66qKabWal2P7d6P6ZRvQnBtohoCKKW9g7uaFh8zagdpG61WdghCH/dSMLkL446CWnWPa5LFTjugnQLAGBF0A"
        "wjHqI319vCz5eR8xuAsfteCTQS3o9jTh7PM6/5iuDcTwzRKxN7Ks/cX9hwiRK9nhEyf0M/Q4X0mD1v+MyYHEXS4/y0YDduZF"
        "AsBrJBU3LhigcUoooeSMX6QRn1YbwAqSDxkr+jCjoV5SXYwCJHcB/sghKeKYXNxl6x7+Ju7B5C6xdhuPdgJF0QWRQ6e9NN+N"
        "7bZB8iCw9Me9yNI+TwlWFxtuqn4Ftf5muwGWY+n1o3YjWDIFEa7bxgNhSHluNjY7LYnNQhfYsnZrawuV69rXhr5vEG8YKXqa"
        "JP2zEyF/snNN+h1KUydYQvMghQI10Zw5DUwWJQZUiH9CqjKHsZABZnDkwad0TfhVLrtDrhceakh8slgH4tB0UdKy6RP+fQ2I"
        "Man4mWtPrqz0UAxJiuZ9gDr4V7djYOZxxBgpKY5sEqvGZSSoeI4DKU9AgJ0F0I+xpViCUpw9T2XwRtsoThHr/lRZV+sHMXF2"
        "lFZpcoTB/lRyiCc2/+kKX9e0aPI0ngLTnKmHR4E87Hr6WKNZ/q7ruIRY5DgPCkdAAv5MGCUMO1g0U5JrMINWBzCmb0CXj+FQ"
        "igXC8MKhhwUgJOCfK2aqxe1azQJ90NGsGJoZCcbEBjEQGc+TPV0aiz9gsT1Dyq/8BE49cFxTmGf+IsZNbbZ2CjB/k2maMoCc"
        "pgnrSrYehG0XaCKqj0lr6TADY6QI4QrsJn/Ow52//u9MqNVnS6MCqzCUke4tBizBKOw4hMl8UtiRD6swZg6JAGyCBW6DVODq"
        "RF3zEXkht2WGKFwUTbHiXaW2mPfGQ4ZSFpQMbdh1RsHCAufxyBOPqozgVXtmNdM87Vj0D4aAT/gTEZJh+oJ8bNuvs7Ojs5Qb"
        "JY+j7Jh0rn7GVkaZAURBXINKHJyPHQq+3w5Ublpp1frhhBLrSpkCx8pPRbQPdwyiRbKfpvVr4FpvbCaP1qpVzsDvZWYvHws2"
        "2ZMOeT7cCz6w/LH/Wt/etW/rqXGM4KI4hRMrK6yWWMi5QyZpY8lJxWEva56b57+NWmya/PxzavX/a7RdL+u4ljinE9/kIHxW"
        "RJbFMm/keutpYQtKufWnGgcveUTmzHBl8okgCq2QJwK5X1VchtUtjIXXKCK0VjrgYAOtF8GKmi83xStM0TyHzjRYzWzvH/ih"
        "Sldr34dBWH8xAivuDTnGIQBoUzl2XWJ/v0t1LO2fIT0VQF9VxZAsvF2pWHymBVzXsuv+7gTS3jnHA05VdlGZVvvRZLiwySUh"
        "9qMNphJjw4N0KAHFo9hmT5SkBVqzS784bn5YJ+lLdrOC7utkPU0j9Q6tfpKOzBxxus957pKN+wULNE2TPlRlQ8gAEEWa0fOm"
        "8ugUtsYSVFnEQheSLgpe3n0eyrpMU5mZHFBu2TyoXk/n60y24QO3iWBimWevX+3D+QZOXpKIDZG44FOhzx16RySlR4Pto1hZ"
        "uwTXCYF5u5DKlmQdC+5sxDMMoM4qy4AzT3LblyUkBQDtODRH5yoUYS8CEZHo99xqm2GcFCvi2J8HTELSfC13A8erfEh9rbG+"
        "yG9aPQ14PydkM5yHAv+rySC5RAlubu3kHE/nzhwpm+1hcAzgSHuXEFuJTmYRH9hYCHB0+P7p24OX796/OfnnF3/U5GT7sSh1"
        "D9OR6IbpP/WjffijffgG5IPP77O5aGCfTkwcwlM4+2WzfbIoP5Z+/7HaVX8/nfcZ1Vv7YywhJi+xHT/aB+nwGdDP60SjEPj8"
        "Pj09cz8OxTNRHr9GEjUalap64fcDuN7Tu861k8w1oKr2jqQJWfGcA0k2NQkFuBk8XwN6TM497RLT0Uwps4npgvQko7eKpqlJ"
        "JJ1Nyghu+C1GODEDmp2xk7n4i0pSgmr0rx22nmEE37BaXj/Svo9v5HO31B50NKKtMu2qI41icxPLFgjk58RfIVQsCeNSRtpd"
        "Fhh0rMzI4bo5b2rho/PjW8TmI+6o8N8ZQ07wM62NhEK2nkl+KPFydr46RWo0nu2OJzehlsUO5FPOwa5CA+eRwytahRE4qt8Z"
        "NESbNRGXlnOPDp4XMo1z07GWMiq8xE3IgU+VR45uzY3Jva01YKsuA0M6OT85BaICLlM1mwPXb2YwWjuGByA6GiWnRMx19Zm4"
        "a/l+USs2JLGSZ3a2RMGBrPRSRG2quhJhgoQM6drjTKhi8hdhrQxK0tg1ijkpAwhCY+erGRiugDDdupyv5ecrdUosuSFczbq4"
        "hrwanKQYMfVjWEHGh7DEuQmFqxdTzhoEFh84pLDdtRmX+Fx02tMYkRBN/Vzs3fvUbBcJKgQMOK6khoGdLLKLVJJswa58yoiL"
        "QVzeYZMjyyAjt4O9j+H2AT+QxH+Qs81xFPPrffQnpjOldb6YIiozSDT4jRdQSlvmHi17UploutERWpZUbGWf0UJRTO7wBj4v"
        "UJIfKK+a66ZbPXaYvfA2EdoaxJP0Fim0dcSe3CGYP3JFRdhTuF2fQEeQ75m8mz+N9ihM0ktghU3C4hOTFFvyW1td0TrbXi95"
        "LbmT1xRnEHGjnwfOTuqnIFYN8Faowj1H0k7PJc2kcBO4iG/Cla0ZixEia47qamT55Pyg6KHDwDZEEs3IssxnfzGZP8fx7Dzh"
        "uzrdrKjGRrYM+fi5cdwy9nuzZl0NPDJcqRLOWFEQ0/Ywz+IUZDK6WGRYGyYhlbfUkM5UeJL2fhp1OT2mXJ9Cpmei89kA+Esy"
        "luuqpBOf6a9LwvQs+vfg8czja4b9LMyyBsvJfOOxkSCmGX5JGruLWcwcu56LzRV+pWNiSfUqdXyt+RRYkRTAjCXpgiBmaPaS"
        "R5hxKFMNf3NFGeNrJmVUHeGsSyIu0GqkedZCZcCF2gU981PR/pIJynq2FSYoRZbxzfqKb9PXsrnrOFCcaJek6/wZl2iQx/ar"
        "Q447XzS1nMXGDVCpcztY+SVBQxgTsgrVHIoblRa7SgLBUCRJlWQaTcSbQcP+itEBv8ctC2SC4kquydqN9grt9MlPHFn9pxE1"
        "iLSvjBgb79bFHT99E1fkaBJWyTR7BeRt0lhgJFZnyXDeglr76WAQQIqXuCJJBifixRBTyd/hgpnRa9BM42YD8sBcBqTtXjOY"
        "2EuntUIndlHha2naOfB9Kt0RSJcOYul748r+4pv2Z4/Bqv7c98aNpYVJbmG5Gt4NCTxRpGg8NPWANQJeMOA/zYG8G/kle5Sh"
        "RRhJLH4MOMdSJY8DkYotpHuoh3L36hJirdXvwMw57VbiLw0f0mGcjiSRpSYEdOOA5tdQuwyZ5kOzJKlhtUlcjxLkAjgJwIOj"
        "vcpEriJiYBaXFLlq45H2GwFvXPYrrOSLebOJ0zF/4ug/IyHnJ0/bbBZd3CYf4xUcALz0JZLLwQSDBhAKPdFHa8gUdTL+HG0i"
        "tjEc0HPU/UiTcPD2YirYn8oKbu5VmqpNsXsEMKaJ5BtV6SUtuCgtMfDi/B6cj8Me5lm4Phh3CAz0eHmUZumOptK2pFzCe+7v"
        "Uef4mFzexz3kru9Rdwc7CvBqOK54GkoJZvPCGUkY/3Ru0sGYM7oimlIbNJTz6rWD21ppRfTgrHCB+6XrVIzKW6S4WKTy3IUg"
        "TwtiXJG3cwmOFdPQNjTJrYpt3m6oFtsSLQEM+a5HupQACqAse3G1211d+OCCr9ZzSUrLzGB6b4+RnmajtDy3i2n3es8+8Nvw"
        "4O4aFj10QBKTMPJr5PHyGouujCYeghlWdkWJ1jTDlSInzPUj4kVVwGzBaFcCgzZ0hJRNGKlRP4pFM+kvy1ZVApcg/myuGhPJ"
        "ZDo4zBxXcqTtV6VKu1b6KloOcFIJvlBiRYJuQe5LVusrAe/KWWJJxFEUA0Kn9P/3XYOKV8+p/qdj/y+dtPbxfOnbmMpDwLV2"
        "dVwhrgLwnXVSYWfz2A6tAo1myD6qR6UuUv+FYpGTpioBLhQHEtkfLyeAu4Fjq2QBd8KXuf9FJb2120y66Y30uFlGsk5pIQp3"
        "dKSFK8LHRR9+E0jXkk4ffyyqf77ylPbHnoYeg9fu3YmsWO3Vx2fV4WGLx1VhMwFEAMRPK78qIrzrtnes2zsWVdvxLwTOIGI+"
        "pMTFDlRh+nOziMUSLQPwjPCtMguOd91IAqQfl2SpYsNiPaFHmzIHy21kwsaIvFNu5CMb6ZlGHhfslli+VzYog6JDwJQZmY2j"
        "wLLlp7FqKFIVj66uT2Q3oo8VNSuAbcO3PnFMXwWK518BufjF9u5GOsjzxs0Pjno+VKFmb9XJEndW5GyQhj27liY4xFggCRkk"
        "ogpO1cGkXXubG42pVgMzSf5/ddZ4GP4xR82qiFVIK6mJywyKYzNVQVfBYLoRcmrVQ1i5jrIiVtX6jD3U0eGR6ey4OiP8zaZd"
        "OXU6CIiEsqRHKQswCpaTsLqyiebrXSE2S2VUKgw5Br9bchvOaORW1+g6bxSP6RQQb6SSRNVK87Tu/pH+mx/k/jWso2n2WNPN"
        "Bm9I0jclGO6XJjuxwd8GjOqawcfMX32m30ryEcMTj+mywtum6Lper1F2NWYHubkDV7Kl9F5kALUwXVw1eTD+143fyFdnFH9k"
        "ctO1/ddP//jti6fP9c6p5M+84IpZHNJkNLA3Yc378vS5SNtlq9elXOgTEbpf25LeDZM2nxA8sxGwt063bfwTr5cyLham3JGn"
        "CS7SD6wKHPazAr01eV+c8k/zvrRq5Utm5KddC5ciM/nznn13JItzrMoFmfWe/FWwlgcGJtsIzHnmLsXSV9Uoob9nQhE8FaQk"
        "gDURxRVR617yNDCjGUPci3hXud9gxarLRaLF9tRNMvQXA5gPb7fW7oKOfrdqGcOIq6Gfb8LmlmAW9YZ1o6rXoMQX34P+Yl4+"
        "/bbyOxAZqaaFb263H5U0vLK8JjdA4zdPpxOkw/ltG4cDwbN/Pnn79M2LiqsukVbKXHCX0Y/FvxyuaS836EYa+2eij4ur7QRC"
        "7VnP9MmuZA2nNLdh+7z3Fs+fPmviRfso7pJaxlWbn2Az86JP2HDo6e2/gLtxrLYt94r+uG5gri2peTJmpks4ZN6XzuGtfJbx"
        "TrxOa0fMZJf2hfq814QYCDZz7QzSnGHvJ2DYik7pluKtRZ7IJX9I96NJt9R9J1jsZmPt4P0zWW4T1i4rB6WWfWSyn66k77Cu"
        "Rvy9OgcCt+jcGwTy/Ln9+bz29K0mUJClYot56m4xjM2rnsXbfs/evsh+5XbD+pk2pj90n5iKAchuqS8w9bYJJNI4NSWDW0KK"
        "nM7mzghPCxdeJLIWuMTUFbjcSENdsJeEXgPX6bg1gv2qhCF4cYb4MByVM2sM+J6+tzjtl41Wmg9gsId8cVwehsC1gekbDALR"
        "DSsH8Ymd8d5pDsYoAWIj/3M8Np2THdcFckVphhIE8npj/Fweox7apjtZ3pkKTlNxjpohxFtYDycorQ9WTMbcYHJ+vLRxPp27"
        "fsEwwVXrJWtiUrQcdTvtII7OuJSzx7qHBejn3kGe+Ab5LYcK+BaoYKutry1CEAYPmbz84M2qOyCqFiIYKSVODogy6FnaNBm/"
        "ZHWWvXfr9BI0qfaLlC6r0uv7CytoyyKiqr1SN3GAjnRdQMyKBSYcFluY6kWbRdnyNZYFdwE3fMZVlZKz3pIZA6K03BRx5R6R"
        "FrX//BcpSQ3CUxS5dxWCJEr076Ipc3pPcYa9G/QCiy9dQuzt7Ym9LX6ZB/Sz91B2DtLirFXNxs8bpfMqsleJSWGviGd1s5zP"
        "ZOpCfsrQJrOMJ2jgSGmL3FzkUX6x+Ys7NBTNulpWG4lfotVD1RT1qkpKbiu0XNOogqKTL/xOjv0AtqN+kXVNrigSlH2smZTF"
        "XDTIjyXIrXQPkim3JyV88JCvivDFnM7pMuyQvXGsuu3HjSJmxa9Rvm7JNrRi4qaxYuLHR10TX+RDj/Mh+uu/3sn/+m9dimW+"
        "S1CTL6Stlmawf//Mrl3TDdxuR3NpKmFaJs+NUfJs3uhg1QvICjOU6c1UHA8ZEC9i0zvCzpsRnEMpGZx3g8pR0Yq5jkppjv0l"
        "ON/+AOLHozH0eRveCF/aXVuZQcIUpkOroZbnTW/XSZEDmnnROPZwFPLWjT7+XE4gzezPvABzbsKykPafWcKHvLSM3l6np0wW"
        "AyQh7lIaIEyHFXN1TRpKQ8E6XiFzSpbbX4KGdn3hyRw7f0XDw+W6OugncLWCNg+BaTCILZwTm06o3LAa4ZHHRFwFinRJ6MlN"
        "C1+XO8LLsKXKSH1hKZE9ZCG2czKWoT9CGYF4kuNs2SVUfZeucZHqGxVjKSnDjaPN7aRdUH9OO37FVXszZwiqikuv0Bw7DpSF"
        "kxWXWPrOA6u1d1xQ8cC4LLqRtWbyeejgZmuF749xJtHzaXhDQbHmjXCF8qJu3limUekBMbF36qwXqR98x3n3rfHoU5G8rejf"
        "Wqg1gU4F3Gr2IdF34QaxCyjD4PDISzwYIwjg6El0zzCeTDK1euWI9xG3QqRrBxxIGCYT0prYoDG4KER1W3quV1y68QRO+6eS"
        "RFz8DsbdkH0QfJFZlTikK0ma5+UopbfnXtmzXx0ECOkBtqO3yEWJfoVrKqjsc8kPhX0czSXTx7XJOYqh2chxL+j+yDTlEcOA"
        "WNqL3KWYp2A2e/up3zWDKUZ+RI6z3zg+XskOeLMk36/lG64bXbinby0DBRDee/r2SERNo1YDddpTEoWZ7nGuez1lo8Q7Q0a/"
        "54HhHv809TjseQhozxAVPtsQZoA5tZbWklwgbFCQVOCNaeGgI1MOAIEJcE0iOMCfATgdHr/jTCLZLYwaLBs94y+5TnSWnGaM"
        "TAnz+dX8Nhi2dnqaSm45vTkgbnlpTC0nORzPTwDdYdoTew59sN1nIn1A6+kCLrqxP1y9n/av//qG+mY76L/+W8uF49g+rAdW"
        "HsZkldODlvOC5syIw4yg2DY+Irek5v7M5V+Vb+n3wjKGV7lTru+VdznQuI3cO15gH3AqxE453WuGvEpVTdXE9QAwwpHqNBT2"
        "+uImX3A9iuWcQzzI+7Mzk6+gWeQooJuK+6FX0htaTcoN3anlIjIR7wepo7XQX7eLsDs9RByT6FwCn1q+uToUmYevIq2FHrum"
        "5pqQoeSWLfpc5FUDTZIoci2euJIOldusajwoOC1svsiL0L4uxLuX0Ko5ulA/v41Hjx61wiumqwe4JGv7uRVIx2zOBIn/Nu8g"
        "EFwsk0VvBNqXXydw9ENBvciVD18Ia92t8KtYTPRoZMwykhiCkwhtwRy3o9CuJeu15AeD7uQDoH27bZXj7G/ZRctbdJ1K+4pc"
        "B6ZgUQQbZ1kRtLQiF6aH6orIOoICLiMZgGIumAXzCNduCjlRAG4cl1z6vUhW/xJfTsw7bMVwqTn0MTt/h7gdesQ9whmViHio"
        "NMAoeVP+kvVjyhh86NkH6jft5VTqDn+NfOqN1uRLkZubZ7yXZdx6L/8o8RaFixnXB3MQjTO2fBcloU/bJBmin5N8nOtRfFmw"
        "GbwemYFgDrGmfgD/V4rXjc4hPb3W067sdlcEpOg1nHY/JYlCwsgNx9sbjk90h8G1KOhWbO0Mm9XoDbmfTiY2dEso1Rwqk801"
        "zITkJ7iG5/R1b4LlShjOay9w1w2bLTH64mkapKzxmjGpa3TnnOZQUmRs7jRKBma7BCYn6ct62qY7CXcNxTt2n5pqioiL9WFW"
        "G/GyzkuuXcXYdQG6K3pEVw0JV2ZKEa9dcZQuJScJZsfkJEaPMZVowILIeZ0X/L1Q4OIiN1S6KmeyuPNMzytuVMNbolwRl/Tx"
        "iz19khwWFVFkmKuTn8x0hfLy0mFpwTsALXJude82YORYOSdkCuIkUO/5N3/EFyUMIEhBjr5ifBlnyFOE110yTwrLyAO9rS4q"
        "L1Hg53LmsxVFC2w9F4dL5vv7QILmBhlfeBnPiIsmg5biozpj3lEtpKlV47qkE9MHn+aZkV2SQ2qvui/eu3nAclUXTL2jCz6G"
        "s/740vCBhp03yvVjcwaa3jerYbff1nzXEdUYiTK7kA7cb7NTApQ8dPwVuIUD98qqgcvoNwK1i9tNaYyyeHfJ8V6gJpbTRCTG"
        "ty78zsCSl8jFNuS4H8CbJsa2UKHnFce1J4bFSSIn9bgc4TMsbq8Zqw+4/LrLXxDymQ1p0hXE7RFjejFnU2YuXkxO4d+cG149"
        "dGWeXLp5uTZlJo0qR3GUrmCdeEVOOX9l4ePKZptRqfHG2hL3YhYOXXilAysFs7Qz7aYsVMU1PYFHkF0yBD5NNPP0hjD4ulLh"
        "JT56eZrPyASJw+BSAnhNx9nV0R3+EBURFRhgb2+Zb1tqwNsLHzMFhGtpByrWenijNR6uraje86ubRMk3Zx6wnqICdRkSbdSe"
        "VYJ2eZk3XAVnsQltPiEDVAR3LB3Zy2608eHo0rMV5qCOTDltcpx6gg8TgOTtpePzuHyhtCS26nNBQyRuohZwJ2/dTwJlB+mX"
        "v2T5RjMq3yltbrpJljBov9+92foNaY3jGoo3dlyhSK7Inmiuu/wYxAN5TOpRv3/s8DhX5GPFAIW6da8PmdArwYotzvsJleKS"
        "257qFn2KqL24YqyXq8d6rGdYtZhJZdDKDZazuJ9LxymXcUnoNRlZ6k2ofIOJOC5fbVYxznNHXVaN+vyqUZ8aMZqtLvuBLlSm"
        "ZEJeJqzriHmu3w9ebsrLitvhTQF6lNiy9Eg8xQnp3+RWeE/6I+LReo2l7FCnazcXyEQToBIZmzxvOPpn+Fp+8w0uuKxsOBwt"
        "3dhZyE+FbMV/HEOJOD4krb5vkhSXbc5lDV1h36HzyLJ151Wgg1PPtiIaSCOEJMxXYIEApOocY8uzfs/EeHIIcOFh4/9jow93"
        "h349wk+rEtRqEioFbs/4tUrDqddP6zmQzBAKA+LRo34xLtDMzC5E0m5+1osy4FqoQ6dukdoEkshZxuM6usYK9cui+i3/KP4r"
        "3iIMJEYWutPnC72ZOHFO6MGVTneR3in04QqI9aCcKUVAAG9Db43KWHFr/6o0iq2M157Mw8tkwlhwz3GxIux5giNbGOsbQZpr"
        "6ixuaDWrVJuo/qIqpjcI6M2Vrl8bzrsygtfXYVNDnVkNddcF8/79P/yn6+J5C0uhzN9T/ZaEJRIBl1Q6DDVxhrylCGdvHh8l"
        "qvOGEZcmV1k5xlQCek2cKbdws5JzZ+Vb9HNl1GJ1qMnl9c71V8QgamjHLZ3lV0QeYsqVEYdFojAunYm4W/bArwgxWE07SxGM"
        "c4D0fPM29/d8gJ4qKdSSBAgktSDdN/pJye2prWfDYWHStrjLd8VT/Yk02a24WdSRi5zJ15RM4ZJmpiKivLOsHai0q9vLVQs8"
        "orc+eaigqS6mFQZqTMFpmg0+kk0kgmlfgTqsVaaS60cTarXGgz2scgUXk1Groh0838mwuCGGyhMWLpmicReGRJLRT671E7Rj"
        "kkvszuDOHc+YVoE6FffVvtdUAX17tQ2v7fkKptp72n3DGVvEksH+jSGjZDt8yehLwViBTJov/Nhk5UOsVRFdS5pI/d2KDuXO"
        "WFqeLOt7J2/hcqz8T5M/TUqGxNce2/K3/5U3zjAEVG5rxv3j+2hD2HlhMwa4OnqGsf31Xw+ZaNs3AHKFlvbboVeftiiiCk2T"
        "svTL3jz12p8mhz7hp2Z2MpcLmaVLb6vs8hdgBGhriNqSJ8f4NVX1cV20TGM5XGbZZvrMZx01HCdd2kRps6A5ayscqvAKAJQt"
        "c6iy6soJGjgI+A+5ZlevdE8qeKZfFvjisVmnjK3aqzborWQ7xAa4UcpbfEX2ldA1sXuLW+z8RpajBCWlQpCqxMtI9jwZJfNE"
        "kpLZdGQlr9Cg4FJ/obDHZXJE4Wp0ukQpr0asYY60q2mk8wN2mOVQnNc86Bkkod+BJE50UJRXAREAWOYnZLZEek1gGg5Rfx2o"
        "xIrftjeDrWygeezZAtFqmfmscjSkIHJC38J6cflDIeLs7QWuiFXii1FP21AET3w1UQ/OPiixD85IaCIgnK2wCGHwpZ8QudjA"
        "Bi0hP4798AkvS77JlP7oUaPhOah6lpmjbnfnuAiQINJJSi6XjLNjZArADL1gv/ms15zzx7EmtAWuKt6b3zJb3lXfDW+uL00H"
        "CZLnRRH5JY1K2AucPGmBMQFiOmuJcOO6TnS1i7UOG5ZNNt9LO1nc/KzZ7Orq/9XgSnzz4lCu767di6fpPW4yChUg8nnt2Zvn"
        "WsDAkdz1aIFKxiWSrr42P/ia2gHzko94RWysb/hUDL9mFBf6zfwI18ELa8QHxtbpSz413aLiRX+UMSznD9+9kqCretGIrdcs"
        "+sOJ2H/6zYuTbw/fvKZuBLj98ReDrM/cwdHZfDx6svaY/+Cw48qBGvrgCzSGf8YJtYoguICXvdpiPtx4yK/zdD5KngT05PE9"
        "fbn2mEHNALQRmoLUgGDUwV5tGF/UojMaz2r3AGFZLSg3pYQwmUAQt4XO5nPkOLl3D64ucD48zUDLEuxc3kIW3rAu7j4bYYuS"
        "5EZ17/XzvPP7YTxORx/3XoE7mHU/IOvwv8Nt7rs7+B88hXYf4H+4xO13ptT7RS89vwsRMbv7bpLYtwdILZzc/Qb5v5P8XNvw"
        "6v8OrDa3ZI/3e3G8Msona91ZBor5CTF58ATZvD+93MUj76MwPz6vfYWvvexyg1cEIak+nnmH2AZe8Su3CWr3bPARxRBCDA0v"
        "mOFdCpAbZwnzJ3eZ/PYOy5pSXAW83J5e3ttsbe/Ax2RW39jgW6hFJL67a9+d4k0PtORUMI193ePrz2ut3unwEu3x9iKhH9CQ"
        "XiaD3Yi3ps9lFD9vpAz7hxJ+czcSizFGjtsxsQ1dsktJdfPDS3RA5RLCWT50EW0zGCQTdrl27ysvehEXtk0lJ/vB4XfPX72D"
        "mQp8CY7FBdlMLIr4DUALdpHCpc2v99U9WYsW5bmNfC4M7CccS10FOMsJHPDEHMB9Lom+e0W78Ee4eY43FszIFk/yDUg/6ZC7"
        "daaVsMkI6ZlerqhdVJGO5GhoRUAWAoRuUNFWG2HxNlrtTjLmq/48vmrUpX57gKAv23E73uzYF0PsDxJQp/Fog/mradCqP2pz"
        "QDvyN55H7TvYwfYdyCanvbi+2dlu7jxsdrYeNFtbDzWIZIIsnmQJ4TZ3J3CsL7f8MGx5E81GRcuPOs0ttLy52Wx1OrdsWcd8"
        "37a8g4YRYmWbfvCo+aDd7HQeoeWdipZ1NYZcHuhIHg17XNvxAlYlvIm3Hg37bdlsYEG86G/3doYDvgDukkqD+4PNYVtbwX0/"
        "KCTddnZ2mvZ/rfb2TkN3kTJ21Xf5DHag8uuOGSS+b5xVlngk9XvzSeXXB/Zrde3Njmm/h8FvYhGhOE4HV3Qz6FxT7qEd8Fk8"
        "4EFu49Yp2Xz8keLtJv+vtaXL0hvRHY5/68ROprJIJADvLx/2dvrD+7vulcCyJMKdFUCwubUzSGCF+PJBfyumvvDLfrtzf7Al"
        "HWQT19hwaA5ENilG9xDj6mzbwflgvm3XXg5gVceP2tovGm5a+CBg8VU77g37DQMtGc/bAOZ1syz1dgRMH20+dP16hwD9ouPP"
        "K/De66dfH9ArGgH2TaELvIgK9w4jlmsMqzZY6Lm5o2os+sxVKBAXUecGAZIKYXt2K7Hh/qIHjXZ0gJNzJZYyGHHbYUShmBEp"
        "ZvROHBWYDzXuz69GjNsOMd6svo8f2yFqvMUABEO+3HrZfvlgdyV+/HLwsP8wwX2RLcwvRCagqYBdQFObF74+NFvroZf2i/b2"
        "5kMfvWx/vb2989BDL1+3Nzv3X/jopbO92d566aOXL19+/XL75csCn3iDFgwiMF6giy9fbr54+PKpQxD2s8EHX6Kx9oNnBQZA"
        "DLs7296Y5dR3qr7Yc0Ro5v/aQTU520Lyw1P94sXW052vrznV9nDpGPnw4OnO9ma0vcPzpU2UT7jpu3TItzCwrWBwVx/rzYe2"
        "a+nR9RbtSNe6UcHR1jl+Lh+vFtbZcnIrxxJVVusCUJHAW9k8/xvveu0hW3hmi6ADAUXePdHVxxFlvS0C6RZRajAA5cBWYZe3"
        "7w4PX7AQ05ELD+WhklhzzSN7jORWPIV8yosNYUjEBZ6r8Iw2FXBawjVHhmuWc3ktD/RbYpfOL8MuFczXbTFMu19AXzWOeXCf"
        "uG/roc/PtB9afqaz/aC5eX+nif9v7ZCh0bcgHKBZjzaFqEZbO46zenC/ufmwCXYLhP5hQymTj7Ye7ITc1cqDAJ7qweZL4dm+"
        "3By0e1v3zVnoPG1/vf1UpA2fmxr2EoNpLLrrP+ptDYYeutOz5aO7YWfQtxTa56bazYekja0Hnd+ckfLWc7NzJR91f5mPKpZ9"
        "8+EVXJTXR+fhzZiozWUealvY3BIPRRAgzdl2H0pbXuaw2ssc1q/CxVvtAhdH9wPsaHCw60dBaLfEfbU93stb0Gs5rwJFl0e0"
        "c/WIqpmxtkjelYesEeJ1RWciBne78RB4S3AbDaMyz1pt1xOOgbEzqEeAcIGB8HVjh1L5TEV0/dGTe42EebGy+3bnDgdbSMkb"
        "8Bw5TZZXobxsgC/CVYk3wZ5XRz2v2OUbt1oaJJQVGON97ud2h3oMjyzBsAQKI/SqvkOBoAGTF1WSP9bvd9Brwyu9ofeZdM3S"
        "yL59SHrn6XwDIZbnqxbDGx02vQ1MT/S2G/2COmtLWgcEWoDm0t0kpHMt5DMCATaKHmhDRgkmjki+08mG2g4iwn8y241UwbPN"
        "hZkijlb0OptO+hDFD7S6Gx/SAS6yBpUjDdgt1DsSqUDC/SVBGH06aCGnGZlqWgi3SVOnZXQrCvJGfUNnqKjFjAFOK6SlHJVs"
        "EFjnVNrYOx39ErBtVelvzEFt+JvlgQazMoM3gZukXzl4bZRQ3o7synYVGi3gcH8OCBHBFALdlRAbjI/TMOC4KSzt8pRALkjN"
        "0Rxf8RTL3qGCvORZz5c3VvYw3EL5ZfRzBKsFthzxaPJ2aZlygXQt7VRfg07D6xPDIc+oIJQno2EBQa7PtuG1Zef9yW7yReWS"
        "uPaVd5QeRnSthdpu16RS7Fp1ndeViKmd1XOU3j+YLaLONFz8m46nJUNaXjBHk8pqSktZtKlpevMZPeCMtn7NjATfV8zpNvsN"
        "T+N5IuCYkN3+gKSndia/ci3Alg98yOXvXfm7AYQ0pWywgeqLMa5MjjaHM/7PQLY2h8eGj6O2LY7qbN8YSbXIwFXOgh8q1qSx"
        "tB36ZRYMxSpNQpHGrLX8ckRJCHyICMkDkSXy8NPqUqWTz78bg3SmCSvoacgVtFhdVwHqd7MwbVkD/CCWlaXX8igKBcGG4Jiz"
        "jq+9Jydkoc+O5GwFOgavStxFvLnh0VhcAocrneM8KYEHGW5ZlptTqbaaITDE1sSHpXQiKFlBShyfyvXNAiioWNTdqUKS5H4C"
        "BuI2cL6EZMprJDsAiRmxGJ9uiJKCg08TTgXR2DJEg0JIfrtT1nowLK+vNFN5SvhhBeYoLWM/HvXr9qzAfUmUqN6RaTsEvgSf"
        "0nvrvLRAQkOugK0V0LgKy2snF2EnW+0lTPtQFjwk+Js7fhOIyZuclga7U42xK7HrsnFJZlm8RtZXeHOkeYHQlMx2/N1q5Yve"
        "jUHq146D7ElyusyHCDZia91I2xSwMsZDT0V0HVE0LA/+rx3Ax8NtBwSCJSp4EFFyGq3xNSB5X5VQt6COAnlGhirqAdR28mYh"
        "KOB3Xp5wz1+s3ijrn19H0beWVy0fY9VWNXQTVstT0p1xXyvPuGgiyuV/DfFfpolW1K7qxU7StJZOzhinDfDU63JxsB9qrQ+8"
        "w/c6SDKAsxPg/a3Ob8McFwI1jP0xhcjdpb0pRpoG9ulCBIeoZth1Fca3Pbmp0wnoVWcFvaoYtd11H2Rbndwb0E22NCjMCegQ"
        "O/erlyrk+FTLfOX2XHmUCcvG9eBGxGXpPFWScYzqtzndVMmtPkWiknPr0JIwrRk8cOQXVLX/sNNUtC/roR4hjstVHXHIqXRC"
        "ybGzfbVgyg5Oz3jfxKcqeWyJAmmVI+PfPThGteIsb++scsYwPX3IZohGPQ3OTnHalmmWa3oz+iKF7zDskRTZw9aciux6BVnh"
        "QOJv1woVVaC3Wdafboe2fuxCjHDcWLvMz8hZMA4JwX3pXFbg3yF8VK5OzfkZO4oMgyvsKj/WN1TbHX0GRllZyJXBgpxh2T1e"
        "f2NbuIrtyk1cKSzT1emaQ64dbBR4bqn1ZUpYISnY7tyhE+QxYPiGWUJsDa5LTQ30sOyRgsWxTzQdePXPwM7cionxPZk6Dw1w"
        "sxn/KGxbDuTGFOYKrvrGGyGjKHOA20s1hgari6rup0U+T4cfN9whsCiYaF88Km+lQ1xmnkVaQWMD8Suz0nnbp2mVK7VE077s"
        "dPo7O0mI/dpqZLAK8q3t5uajB81H28bEoEyk3W1RldMbNDKjWYV9G1d24uufw/M7XcgloisPsH7/JLYzHwNu7ehxhJshVE+n"
        "Jdc5KDUKaVWWqkB5qtQIeHk0sqFAazzrRM0I37qdnWiRbsBemknJZvQMVANmVFA293JJgSQWsMbubyoH3i/JgbR6KpwCTjg3"
        "wNrVjnwl2bZS3PdH7FumaC5zjoCP/D6vxBR2XFZ95OmaxGogWz1mUGZU976J1bSB8lb5tVoOVxigB2UrGfcS6pEz3O7rvfiy"
        "9wd42JaHVyJyfgOmT7PUin43DdIyVgQTLDJYF0w9VOd2OKIuJszDh7AY2hfGklDSBO3YsF3aGEwHvuJt86FTIYgBovobHK1v"
        "b5xYQsumIauscGpyT17sdPwuq0X1B3bAtycHttbtdLydGzC0txZN1yJ/3vdXyslX8LjefHrXCJlbwex/tVgqzdxMIpWi/0hh"
        "1HVwUzkU3/NfqNd2eI9tbN24EVx5kyCz/laTTTWq2kKCnUGKOAzTsPxaAdmrdMf3K1Vzs8npCuuTIJbKM1x1cNlOOpkyu0IL"
        "8dH2Wf5pzS/nzgjI5Jii6b+eet3IX1wOkFMN/RrK5RRNzDtBnGjphcV3Wxb3cLItZt5p6j+3QxftX4EuVsu6VaeZY7uZRMst"
        "uwUCt5yy2+mCGXy47X0zNsYb87w53FRksDcnJVYiNVVlNEeMNdmT4L9jn5AYdBL07us3TBvYfp2S53u/czOI9c7Xg04ZZYs7"
        "hrIf0/ntGfElsEKOvxkttzjuPmuDtlvz6zG+mquLCjdC+lpp5fZBxLg1wQWHe7FCGeBEigfemfkFR3wlu1sVBqID6iMyLM5X"
        "KPgK1tVJQJRvwpAYu13IoJuDHqUzo9qfdla0Wq018bCmrP5NoDCQXB9UoJv7Rp2+gql+sFPQW/XkrbQvLLPVuGkAAb43hYDN"
        "W2omtPG0wk5oQNUxpoE8ulmxAFuFNXnDeEkJZAMe5kirMrJHdmOzUHj5SjijZ2svOdmsEBweOsGBBNzR8esFCNIaK8mgGjU8"
        "qwSbkuRA0/h4ln3Ib8knOC2I1L4xD9MRua/VEQvkffkrz4q7PFxdieYqzIiVut5fK6YW84Ia3NNg3A8gpkpDvRMuS6Vipvi8"
        "Cple60W06dAy79y90jPIlbmZCc5KUMFUO1VTFYxxA6F8LbpG8Sem1ls6J/lT+pUOKnYvhEI6cu74/htzlzcBt4cBJqDy3PeW"
        "s0GSpSFlEhp/lfdKparPNCJOBy3NAHoLZuJ+NdItgwtpFTBjflshZjsUYpz2gAlFP4Vqon8ERf8lmM5wIO74FzoGybpyQ2pm"
        "W8PWdFYcW3NEytUr96qSjDwqyIjFzleiYyucfo7cZl4nx5LoPL5nAogf3zOB2RLd2wefke/VvMDWGpKizeMNvmFotLxCaQqn"
        "pjD9l2tPHt/DKxPmzcwv+o1XdD4BEnmcjk8lbFtCtXm7uA3b5s3duDlNCvmN0jETrXqv5Gqqcog4O/ULwZOz9iRIKkGNK/W9"
        "fSSo+T9sxpK+qamDDrtWJ86a99FgX/MdbnUagi4P4q26h8u2komkRnGXLQzii5hKuNim0Ks9+X/+p//2v4kOsDEAOfP58T1t"
        "vDwIcWjEGChePXmqETXYM/6yw6nYlwP517VZUZKBP7Unr/H3qlLimV578pb/FOXMDt/TLWYsf5y6VSHQ6S6ai5jsB1G/0n1N"
        "vuL7WUen5fa19mTTTC1yd5bZdFHorWPq+VuErA9PjB96sPvAECHQnNeeHIb3JHtJDZfB5yKitlG3dz56KxD39//w3y0XhOeO"
        "LfUK6cP8UhZsZHAh6IDF1Fq99yYpxJO//9f/Fj0116f4wIC6RUOPhRA8CZMR1ZndhFmWIARnvMTxNP3bv9hsjI3H97TO0tqp"
        "zlSHIflg/BH7BQ1v6UrKj1WFybXicM4YMD//2790kWQlZpwXIDfPmaSSx8OkakX+lVGRv7We5K0otTdnj8CwMyPY3/4vW5VZ"
        "YmiJZ2wqTDeNllnaN/biEl56pLeeMTIA/IFkqx1B+401CTMUTRd/+88uJRLvQLE3prSKw37PwO8vheWOheU/LJgz1CS+ntlt"
        "qwZoQd3VIM3MLU8UAOw7MBW1JyXQUpcPA5PmcoEnj1PsF/9nj7A/VmnjyT5LogC+kaF98mcd9QyZNqDpoG0h1iXiWdQiFg/5"
        "EFYeNlRD7F4UR4Ir3XUFkWhuWKAHLAtrRTLdq+FGgho1K3zqWLSXM6mYXE6gObBj1/HyOfvVK2ZvFbjJmrmLdrx1YzK7hGDn"
        "LtSRq98LPFa5dr/xJORehpvM4IXczbC87SNzuyDvdBr9HP+XGbXcw3GTUR+GF6Z5w9e031hseDtaxKIHb5JdP4lVCJACfHAo"
        "zdSefM81QrLYWG9knMnN8wDwKOGdP1xah39XoV5NrbQKlm1Hz811cAL+Qa7aqvbLh87ddLDizO24M7dTnDkQMpuVXvkoQcaz"
        "+OICLNQk5rJfdQrLI9eL7G4/dHsRQ+XQLbLYDIfN28HcVQNqEwWtWTXeK3a9ctP3Y8nI71IEX4n/IFUWUM8JzZ6ORgrEi7lJ"
        "g2b4EgHjMjMmVQ4SrXLAPM8LJq6OhQ0swD7GvP9TJH8LMA/5tarRzSyPM/s6u6wZzShb83ZAbkZVpQCvs0G67Vp7s9tu6/8v"
        "HWmCqGGLz060rmGN9+E3BUTOvOcmpxTxumOK//4//MeQ7zHXoPhjQfqp5ZHs3GQkrHn7cVwD27+b9PLpbhUYCxsUiWhFGBaf"
        "EIzwPW/Ro2yQ58iGiSeAMR79WwoueFYko2E2GcRy9ekFcEvCMSKoxHIoK+B3ic2MjPOiYTefmmRjT+RqwJ9/ZrJGSCEgYu4e"
        "vRLzuTQpgYnhBwctf/8f/yW64trL4hze5r7LX8iG+aAN2RwggXCTpV146q4sdJkHhRsLKI2R/mxtMSwRzSgSscDg11ix532m"
        "OkHKZKpro9Hf/rcVgFdqeHkjxW3TbOOBScBmQfo7c5uF5ax9jsNmaRdQsxc5Ugb9j/85eg/UQ9IVVyELfzWpNzFnCU9EFk8e"
        "G7uJSAUXXBl9EVI62EAMeU+nFVQuQJVUwWhhfXzy2zHjW5YZlysjq3nvEOHfinfx8x/fhIE5wGmBnFLOaxlplDJOJnF7gbEV"
        "odskmRd6e2x4L0DVjQDkQzANpi6bl4lN42om/nbTl9T1N5n36/giK+40QLVUYTUkguWJS4YQWReCr9xGko2R2vOqKayAMYMV"
        "lk8XDD4WRUrixfK5x3H5X6LgvlEnx4U0a6Wk/x2zNzo10e1S6OpJppLg/46CnMfXndsVOOmQiZUVIUNSkcyuXXdzRLghZsWT"
        "nHfIjpUdlNX/DXC0yfwZQgo8PB2a1Q+CEUxRvRS2FXxf0tfdApPSL69GQQ5Tj0o6xYqlpcbOaeYM78QHFMKn0mI8vkfFmFGS"
        "siQyQNfcmVK3REdGQ43LbCC0K9RyMjE2JIdFnim5mj55n8YgZNjFQQFAYJXk9w9wl5clif7+3/+f0UEfV8zAYs/n76AegxcU"
        "FnEaYuTHuZR6slajDAPtZYq8M7trICCwO/wT707DzVJwSMr64ljINOcvRgkfv/74Ctc/MiRUC4Oq8BYklj5AM5NTXLrbAp9B"
        "3q1+7+h3j5/Uju/xkheWqH+q/Q7ZR2u/g+ZnlxlvHsuv0Vx+PJEfp/JjvbbOH39eZPj5GfnWmUoD0Q7I3MgrpeDmSD8kqNd5"
        "DeaQqZP1vqnXcju2fvcuftBPMDvwpx36NJP7qerw4EQWYgxvmADR8ecn5Cw9y2CGqO2/OzisaR6lbvTvD969beUySzhk1weN"
        "zw3o7FmnLvU/fXbLMsQI2La8f9fjBSEtcjynk/onaly7xdqKzzNVsTDxSVg7krFNxA/0n+qicUYnhBamnm7RCRyghgTgSIva"
        "+Mxro0cjuX0EXQ8XEz2R0wWyGMNJBVeiMhrIm/xzGAhasPXXNTAKabMlmy1T+TZL4+QUmBFX2on+8hdOT2xVrpv+WC6yArJp"
        "QsWHXO0NSWeCJMl1bNPvfofNUmduPH5hCmj2YJoZVwwLX7yBoQsM7BP+6UbaVVf7E2cNHeJnqVT0arv65PpH/nHe38LAg2fZ"
        "eMx8zvZ3XabkTQoNv+atDSOdTLhNshFUEjNtv9hMeMP6aHepoL+fvEdV7aq4sl7Bo4cc8UUNiKyzjwdiPswoNdZrQXoDpH+P"
        "eh4M4LLUU+bpxhdAZqkrpDIeSQ4SNyHkzWUS3ToXZOV/uMZaMoNYa0lkhHdQC1a2xNvZOhLzohfP5Go1nVY65uFLPkSvmDFF"
        "NzMdw+I7yuKBOw6f1sxtIKhB5OHWoY979eaJQTT1mnKZzOjOM90nEnrGGAhcl1bD/dvSOtppiR1NSqi1D4/ILbDr9UIs8Cae"
        "n9F5DlkaHyKMHsOSes3I/dbaptnTFqb/QeeRjnFbDIttuGq4aSHnjeYd/4vpvvjkl26WSmg3OOLMa9xoncnFfZjDPMNhiL97"
        "/7pek7wz96YQ3aX0Z7Oc9JIG+Kk1LdjqEMS9wygxC18QNhRHNjSoCkvDdM4YBEkbTsLrFpf3mUaa8P5Lqcn3IBjANIfZVDCo"
        "h2ttEd6cgYYM+W4sNyQn8fdR/evvDv54ZH4fE6/UhFnMeFVOI2LCbxPWslt9/IIzIJnDm9EXX5gGG6WDdgSkkSKaDcfNYDc0"
        "NktxC8fXh28bDQOL/0SK1rKxbxiua4/4pC8rZ17sehWWh2Mi1zAih3p4tZpuH3YqpqUmKvB0Rvwlg0Bi/uBgyPXCH2LEEihV"
        "MniaWY8EIca4TA70aZLBapuBY/xsAEroo1actTR1vX5w1HCX20QGBUAnHAkvOECe/12b4b7+hVJWAomjsVLCIUfukUU53EFj"
        "jmzsVtGsAF+ZmkLhZF2FeDd2pSdFxppkPkDW3jv7ywA2E5pzoMtzkoZlyLQHwv/TUOkPCCzJPrQk/znpG9L6udu9tPvPkVD1"
        "iDfQf5KO7t7V5XuCy7JXd8W6XnfcXU7Yp4S/R/aCdiS+f4qlfxVNyCbUOZwXmNVtUEgWSOzJEnB5d9eA65/zFGQ+J1kX+1xO"
        "RB+ElDS6xY2lcwoIP6d0S8OlZ+BHY7mFzmR/BCVIL2gFH5mm1rhypAZAZgcIxOyfQaEK+2MdblMSjgVI4FssZAyGRgJiOC+3"
        "BvbBUAQPpOBx4irsrpkdxcsXdLN8LeomQEptTJ3bKQ9MosvDESWyNNwUfdIVcivnv8RyoXkH0svLfTW0h8tecBh6aZcc91ug"
        "KjmZinjMQBpR8NONS/gyxYSW9itbRWwj+ckIA7cMLeLllSOR6kUnEUiOame3rO94fmBFgk8e6cWlE+DEc3GAYrmx/QJn3WxW"
        "z0Ey77dxHi+lMm4ZBpm8L+Hh5saJMW/A65LhqvPKS1zJ9vuo1q5FDAvm/UuXWPuXjEOrbxYiSK1F0aJZK+9BctrB2jXprgXS"
        "jJuZyHXvy6J+8pgixgMapw6jcctTGt+h1GVNXpB1hMrAwLy6fi5WebnvSq4kNCLSSGhsqnl04N6V4p6CmblKga1gbab1jOtV"
        "C1wzLvbWOdsMN4dx6utUrPAFZK56hhs65UpA6lHwUnAi3uIyT6yK0acUhTu2sP1gF63mpF9Asd5sVGtUcasYN0TS0QBYuBr1"
        "8CphXdMC+VwQsQNu7dLqv/WwgHdGIq8VLGvIyHqfGuL5Fw7Q0GRvjNeyzBdCno3ceqHSjbaIzjtODLa8Us/uY4idAWN62A2c"
        "9rADfh8atu5Dn1gFZSajlJcgw3WwKYA1SkLwM0rDgUAfFUkzyUOol35/QqgKXaTw9/MNoY1V4XM9OQVP+nv9RdibB7Cnuol1"
        "sV3Kacullf5oMQAywjViDYJYxHs5HBStK8SmCrEooy9VCbZu4ZOwOJf8gVKJ7llafCI/acsNgNwr69nAylWW7GAeJEu3XR6I"
        "Qn+0Tl3ZutGVrVtd2fqT0v1BRrtUW3EYKgi0rlg1ZXY8i91mr8nLpfN1uQp2LwViewA2ufyot4rw6A2Y5mCxW0Mjs8kzwh5P"
        "4jMDhFa2sKDfh9JMuBoF/qNWq1UB9N6MBfqPBZB6rHG3AP60EeBc+fe7V1eKooT6sg4bpGEQb8Rq4CiMKipxiJ11BXE230qM"
        "g55Wfqa91f88S8ZQ1/klaAEpM8ohxiraqR5CuY/SEMojvHYIhtkMxuABE6+fi47UmAvKB3PocSHmDBsVXBLu5pkIk6Sj1oYL"
        "+RQ2VZ64YTWf4V8bNGzK+jhph/o63Kc82ad0jrx1S6BwAGkI+kIZXtVaBKD/Rd6S6K8VWxeWLRUuLaH9arZP1qqh1zOS+WhJ"
        "Bg/KNTWpjjX0v4LX1W8BcTLL8OpKRQuAG2p4gnU/G1gdirMRJuuGiZ5nDp1Pz1R6toWsqusLfMAopmdgov8sSEFX2tewCbrR"
        "9ce1nm2yvCg/76u8hMdhmowGxdTkowdZIs6Y7dOOCixhdhDX4jlwqH+SRe1Wbk+ouGxKkpRuefHp0DyWy0uzbrjw5gvZcTMA"
        "qhF425a5Xws4/nVyepr5aIMaBegM1RKOAk/FeafweaLFRapYu4upQLsQigdX+hojkJTAuvBeLhTBDXenrojWpkCKL2JoEJ3G"
        "Z6sTBhsfDtj5hPqDdJb7YiDGUtW0vaqFyXWlpg3XzbN3r9+9Jwd+VDM3oBAPfDnceZS0e/KoaTrksZ3EO8kjeUyG2/hPH/vb"
        "Dx+Zt3Fvq/1QHje3ew/j+7Vj29HBD9KJ9T0ksnFOdfJDnNPkSRy+8OSbT/FTb8JWxf6+09tDsOTVw58+81bjvnnSkAhR5vPK"
        "tcIKwF9P575ePyuIV1lPE0KhrSABGVS+sYou35GoOGZQx/FmxLwlljOclwkOONJSKMdENkf1Ow1cKKgVDXflFuliotpitX+s"
        "arTcnqjbWRPb+z2xbx+mBv72mUir+dcc4rigbOY0D1yhxq5bKU8pQU3sPl7jdoeddmPX4RmueKj/L044T+Qneg53DXNQVz9i"
        "vBantuK1+rg1dA+78lc2scs/ZhO7+o9mFXdeot3oLmmNcxo1514utha/MC3gHNyK78ZvzHy3XmTm++eyLHNO8njwAxigo3Ne"
        "4gvh+LzhCZlZgNUxzfdwgrZrrNASy52g5ZWIF4qovf0VK6uhMdaduhHw46wsjFNfOfDCNLiuFQxf3Ve+um/EQJ+ZzT84VtYL"
        "oJHSBGsoAkwlw84aVrym7k0Fry2AWicoh9z2967jgs9m1aAhNxgGejm23zmHr0svR7gMBUpxZERAPNGOMozny1Lvuc71XIYt"
        "8kedcMQ7l3EqoH8DLTsXCcR0mk3WPUnkiatcJQ/o+3D8JnDLDPpN4M0bOxdz588YYy1NkJeA2N46ZAfxoRT5oXAHfnxPi7mu"
        "4oUvbYVt+GKTThrtmDm35ZJafuWsdbjQfHlzhtPC/xxVSk12COUl0Fa8VfD9QNblhK5b90xxyFxn8Fa2LhKnLSADNZiZeEt3"
        "5Szpn4fDo2/Y1DpTgmqFHrmrFQ+MhhLJ3js+hZBUtiPdRQknefR33W24vuDGIhVyjB+guEq1aoFwzxdwGP5pcUSJJ4+WO4N5"
        "RJ5UVhC2G80Z3aTZbzQPbgdCaWIuDL9Js0GGBGzTcdiDnYIlov1Sm2TWQwHR032Qi+QzNtggO3/KqF8yalgK4xs2hLqrVaK+"
        "bNWQu1SrjRoNa8Mo6DwU6r8nopVrXiNNNgVTnQzGV9x/LiTaCzVywXnNl0e1lofZyc/0L8AuUOH9g1oHv/XffSvmO7gLTMku"
        "mBUbJNCxJvtQPI7eU63NE7sphPXCmSZ/gC4TlXb5zhkpvzUvXf+n2teSnRPGyBx28qSO0tK5vBKK/54cQFvuvP4Bg/UIep97"
        "KE2Np4xJPiCNqAf2NGc3/2Kfg/5in0lec8PB2PN1CgZlNJLaHF7O4cE3hxrfj98TPus1E/WsI2UUKs2TTFjiXWRU4zfO6SkD"
        "I1lAgyJru14vhzLnA1F1FVy5eq6Oed38amdW3gmPNVC767f8R0U/dTAMqS7FukJK+uyW7EdK18KnDaF/3m/FNOPW91s9++sr"
        "LvI8mwtPWrY0Qo0MdqQvCnxdyRVLCPB1pBnQbq4OxL075n+8bjJYFtnkH+u81r6D2Tl1OujpjwT1jYgf8c9mg3NHptVtz0iG"
        "XjBkRS1186REBeQChXO7CCy83xIWroCBKz0GVI5qUjPbl9u+cliF4tuDTYGqKQlcqpWSOv4f8M/du4W1Og1tBpfYHx6t0qjB"
        "lpasDnZeRyk16TAzqLFh17iOecuMm2o2ac6/QLOb9/GjaR6NQFxAy1xEE3FxplD25aPteKtHSUkCmLq+yKURIl1fyJJoom4h"
        "jC2BE5Qp5waWGGTDNQjXdN4nB0tNBNN5yPnygGVoQAH3OSFrww7dHMwMtP1Y/JMMxy90Jidw0HAyWMBuUucFkUI2YqxYr0l4"
        "sYht1bEK+FJqBp0y12AWvSuhLqahnDYJq4s2LKcfNJKUGNaSTUzGSm6qTrvZxXFDmaqKEaSWPbbR20F+EQ3ClpB2Eysuz2HA"
        "OjOLhMy1ARw9x3cLHjt9EoUM9V3DUUdmJQQq9Rp2JBAAZF0AFLEZUuiORobovH2OzXKqS7ObQ1VNmvm4Z2ep2QUM7K0/EWCU"
        "tYWyfamUAt/6k74f31ZZUgF3/Uni4sgqi+kh4Lh4LPzpFKqjoZBxUAfsGNAqJj7lQQ4pL1bGw7xWmp733w0tkrY4YeiQgi7r"
        "fms4pZVxGL4e+lLekYdEhkTyaOSraItW+UYzqvwIdEEp/3576fswtR+G+HeYqmRz6blEXjZaSJJwIHZfIHAYLRt2d7vGMonZ"
        "L2tmTeIGKoRDG3Y1wbY+DFYRvMelhkEb3grgVX/EzHKyGO4wYlXMUYSB8TGXFRrCIdBuTuMiKJ2yTXTol8Wbt2SzW+Yg8cjL"
        "IQLusZ+Yedx3tnKdY5c3MfXSJm8wDZSA/vTSa2UubkY1JjKpacehNxHBABvq25DqmBpGn5+qBVQPn/wSImttO6rFqxnL/lWL"
        "Du7qolCKf3ILsbwGzNNTu6JBESTC/dvnWEu7J9r002xOzZ5OMNi8hlWuS0crXR6gT0xFeUjuVp0XXBBJlYVCes1NgabRqZmO"
        "KtVG6i7jWzPVc8fjo3OjCUHKGlEhO7PPbknxLwZf1fYZ9E5t8AuD4ksYP29JUXVENaj/3KH+o7v6bJJs9m/YJEvepEUrOeUm"
        "NwsZqFWaJcLK+VWmCdBtYU7P/Q0x0JFI5hIM8HkyjBcjcYla3Vwgcwp1Lw/oyNOpAfM4/Rmfra7s2I3ZGTkwRNPvje1Fob3A"
        "M1lYwwF5O/oNHs6UBy/7LVbC0KxvOA6TnKHsWJi3MiqDcr2VzjvpJeeUmmtGsjesbCZQzRbcibXEy3uLc/JA0VdROtZ8KsY5"
        "I4dDE8bze0VayE6SU8skORzUxBDEAFj9Cf7S9T8K5lRktKgVEh+0tphJyUndjpHI0vjeiQITo/xLrXgn3LGzGaWFs+jhzLqQ"
        "y7bh065xX1BlQLO8ZobHMwpt2RW2VQsn12j6o7GuvNquKpGbpeVtBkNtBHoX43vKYEXPyofyGq5ott5ZViqcU8UOw62R7BxW"
        "ykSkCMOtMtmL1bJnrfA/N1oJzlgVFg1PH+LEbeusKqMSY07ofaqQItjRMyvyDBnfsV2m8zEhHGCyIMtLmhbklUZIR60GB68B"
        "3ImxWotpvdFV7rHGeFgZdrAVXfEuYYAQNZ43dgtssRu2e8L5naCrOSAOuyEv8RPvT+u1zUb0lNDrG/NNmKIR7k36kiK5iY2T"
        "EnBqoLFOIzIqAa0C1BXcSFqbWj0ztubPpRwcETXG3tadLhByE9Nt0cWGRtF2I4z4ahnHEeiuuq6rbHJiTHdmksllP0GazRfy"
        "DzNRuaJToGqzBwwMsltg9kOVeIvJCex79TozbjejR226vqUDsNIn/GsCKvb+8N2rF4cNtKXtrP2/WmWktA=="),
    "zoomtonic": (
        "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"),
    "beatonic": (
        "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"
        "s5RLGUfc9BxRTkzCZcaVN+bau5wQs7CUMW56xni752P+2ExZ4x06IBzy642vm18/3l7IH+/1nnSf9DtAs2J8KTPBmQraBTU1"
        "4UmSVeWPiL0095qb609i9rL51ebm1pOIvXzVXG892ovZSwv1kza+jtnLva+/+nrz668DP4k6LRxEaDywi3tfr+89+XrXMwj3"
        "tfGDe2is+fhF4ACtxpbf21GfZde3yr5x+4jUzP+ayWOyt+XIT3f13t7G7tZXN+xqt7m0j/zj8e7W5nplc4v7S5vI73B7d26T"
        "b6BjG0nnlm/r9Sfu1fJG/7bKlrxaFyrZ2jrGz/nt1cA8O0luYV8qpY+1QagD2gnz3zH7+yns4WN3C14gpEgQQVv/HBINuUEi"
        "3SBLTTqgEtgi7vL6DexqvIkGd5GhIlZi+BMkURiRxaCeI+FMEJJGcDou4jPaVCJpidRcMalZ9uWNMtDvyV1av467lAhfd+Uw"
        "zW6gvnIe8/gRed/Gk1ieaT5x8kxr83F9/dFWHf9vbFGg0as4OHBmPV2XQ7WyseUlq8eP6utP6hC3cNA/yfRkitnW461Uulq4"
        "ESBTPV7/WmS2e+u95unGI9sLrd3mV5u7om3E0tQZ6pMrp3Hsrvv0dKN3FrE73Vsxuztr9bruhI6lqWb9Cc/GxuPW7y5IRfO5"
        "3loqRz0qylFh2tefLJGione0ntxOiFovylCbIubmZCiSAM+cTf9FbsnzElazKGH9Jl680Qy8uPIo4Y7Gg/17lIS2c9JXM5K9"
        "ogm9UfIKLDrfo63lPSoXxpqieZdusizl68rORA1utztn4FvC21jHRcZZrW5HyjE4NhNAg+GCA+HbtS1q5VNV0fXDKRocX4rw"
        "4nT3zdZ9djZoyWswEZ73i7OQnzbQF+kqJ5tgzct9dAtW+dat5joJYwX6+IjrudmiHSM6loAbkDBOnFe1LSoEGcMgcUz9Xe1R"
        "C2/NorvX1BXStqmRdfvQP30/mK9ddmbvF01G1DssOqATwt5gFbn7MysFqwMrkcyZciN3zqGKyQQHsBl6YA0Z9jFwWPHPR2sa"
        "GFEh/fen2xU18GxyYiadXk/sOute+xDDD/LEr0mpEXzT4hmwHcw7FZbc4sF9jySMd3pqoaRZscf0JkQm0KZlthUleTPfsOp5"
        "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"
        "K/8/HbfRFA=="),
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

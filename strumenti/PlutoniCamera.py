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
# Timeline, audio e ricostruzione (comune a NoSilence, ZoomTonic, PlutoniCamera)
# --------------------------------------------------------------------------
_RATIONAL_FPS = {23.976: 24000 / 1001, 29.97: 30000 / 1001, 47.952: 48000 / 1001,
                 59.94: 60000 / 1001, 119.88: 120000 / 1001}


def norm_fps(fps):
    f = float(str(fps).lower().replace("fps", "").strip())
    for approx, exact in _RATIONAL_FPS.items():
        if abs(f - approx) < 0.01:
            return exact
    return f


def find_ffmpeg():
    cands = [r"D:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe", "ffmpeg"]
    if os.environ.get("FFMPEG"):
        cands.insert(0, os.environ["FFMPEG"])
    for c in cands:
        try:
            subprocess.run([c, "-version"], capture_output=True, stdin=subprocess.DEVNULL,
                           creationflags=_NOWIN, timeout=20)
            return c
        except Exception:
            continue
    return None


FFMPEG = find_ffmpeg()


def current_project():
    return resolve.GetProjectManager().GetCurrentProject()


def current_timeline():
    pj = current_project()
    return pj.GetCurrentTimeline() if pj else None


def tl_key(tl):
    try:
        return "%s|%s" % (tl.GetUniqueId(), tl.GetName())
    except Exception:
        return tl.GetName() if tl else ""


def tl_fps(tl):
    try:
        return norm_fps(tl.GetSetting("timelineFrameRate"))
    except Exception:
        return 24.0


def frames_to_tc(frame, fps):
    fi = max(1, int(round(fps)))
    f = int(round(frame))
    return "%02d:%02d:%02d:%02d" % (f // (fi * 3600), f // (fi * 60) % 60, f // fi % 60, f % fi)


def tc_to_frames(tc, fps):
    """'HH:MM:SS:FF' (o ';') -> fotogramma assoluto di timeline, None se non valido."""
    p = str(tc).strip().replace(";", ":").replace(",", ":").replace(".", ":").split(":")
    if len(p) != 4:
        return None
    try:
        h, m, s, f = (int(x) for x in p)
    except ValueError:
        return None
    fi = max(1, int(round(fps)))
    return ((h * 60 + m) * 60 + s) * fi + f


def track_info(tl):
    """Tracce video e audio della timeline, con nome e numero di clip."""
    out = {"video": [], "audio": []}
    if not tl:
        return out
    for kind in ("video", "audio"):
        for i in range(1, (tl.GetTrackCount(kind) or 0) + 1):
            try:
                name = tl.GetTrackName(kind, i) or ""
            except Exception:
                name = ""
            items = tl.GetItemListInTrack(kind, i) or []
            try:
                on = bool(tl.GetIsTrackEnabled(kind, i))
            except Exception:
                on = True
            out[kind].append({"i": i, "name": name or "%s%d" % (kind[0].upper(), i), "n": len(items), "on": on})
    return out


def section(tl, d):
    """Parte da elaborare: tutta la timeline o da/a (timecode). Ritorna (a, b) o errore."""
    a0, b0 = tl.GetStartFrame(), tl.GetEndFrame()
    if not d.get("range"):
        return a0, b0, ""
    fps = tl_fps(tl)
    a, b = tc_to_frames(d.get("from", ""), fps), tc_to_frames(d.get("to", ""), fps)
    if a is None or b is None:
        return a0, b0, "Timecode di inizio o fine non valido (HH:MM:SS:FF)."
    a, b = max(a, a0), min(b, b0)
    if b <= a:
        return a0, b0, "La fine deve venire dopo l'inizio."
    return a, b, ""


# ---------------------------------------------------------------- livello audio
ENV_RATE = 100                 # finestre da 10 ms
_ENV_CACHE = {}


def _clip_envelope(path, start_s, dur_s):
    """Livello in dB (finestre da 10 ms) di un tratto di file, con ffmpeg."""
    key = (path, round(start_s, 3), round(dur_s, 3))
    if key in _ENV_CACHE:
        return _ENV_CACHE[key]
    n = max(1, int(round(dur_s * ENV_RATE)))
    out = [-100.0] * n
    if not FFMPEG or not os.path.exists(path):
        return out
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", "%.3f" % max(0.0, start_s), "-t", "%.3f" % dur_s,
                            "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NOWIN,
                           timeout=max(60, dur_s * 2))
        raw = r.stdout
    except Exception:
        return out
    W = 80                                     # 8000 Hz / 100
    try:
        import numpy as np
        a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float64)
        m = min(n, len(a) // W)
        if m:
            rms = np.sqrt((a[:m * W].reshape(m, W) ** 2).mean(axis=1)) / 32768.0
            db = 20 * np.log10(np.maximum(rms, 1e-5))
            out[:m] = [float(x) for x in db]
    except ImportError:
        import array
        import math as _m
        a = array.array("h")
        a.frombytes(raw[:len(raw) // 2 * 2])
        if sys.byteorder == "big":
            a.byteswap()
        for i in range(min(n, len(a) // W)):
            seg = a[i * W:(i + 1) * W]
            rms = _m.sqrt(sum(x * x for x in seg) / W) / 32768.0
            out[i] = 20 * _m.log10(max(rms, 1e-5))
    _ENV_CACHE[key] = out
    return out


def track_envelope(tl, ti, a, b, progress=None):
    """Livello della traccia audio 'ti' nel tratto [a, b) di timeline (finestre da 10 ms)."""
    fps = tl_fps(tl)
    n = int((b - a) / fps * ENV_RATE) + 1
    env = [-100.0] * n
    items = [it for it in (tl.GetItemListInTrack("audio", ti) or []) if it.GetEnd() > a and it.GetStart() < b]
    for k, it in enumerate(items, 1):
        if progress:
            progress("traccia A%d: clip %d di %d" % (ti, k, len(items)))
        mpi = it.GetMediaPoolItem()
        path = mpi.GetClipProperty("File Path") if mpi else ""
        if not path:
            continue
        s, e = it.GetStart(), it.GetEnd()
        x0, x1 = max(s, a), min(e, b)
        # (GetSourceStartTime() da' il timecode della camera, non la posizione nel file)
        src0 = (it.GetSourceStartFrame() or 0) / clip_fps(mpi, fps)
        part = _clip_envelope(path, src0 + (x0 - s) / fps, (x1 - x0) / fps)
        i0 = int(round((x0 - a) / fps * ENV_RATE))
        for j, v in enumerate(part):
            if 0 <= i0 + j < n and v > env[i0 + j]:
                env[i0 + j] = v
    return env


def merge_envs(envs):
    if not envs:
        return []
    return [max(vals) for vals in zip(*envs)]


def auto_threshold(env):
    """Soglia del silenzio stimata: tra il rumore di fondo e il livello della voce."""
    v = sorted(x for x in env if x > -95)
    if len(v) < 20:
        return -40.0
    floor, voice = v[int(len(v) * 0.10)], v[int(len(v) * 0.90)]
    return round(max(-70.0, min(-12.0, floor + 0.33 * (voice - floor))))


def speech_ranges(env, thr, min_sil, min_speech):
    """Tratti di parlato (indici di finestra [i0, i1)): sopra soglia, pause piu' corte di
    min_sil (s) unite, tratti piu' corti di min_speech (s) scartati."""
    runs, i, n = [], 0, len(env)
    while i < n:
        if env[i] > thr:
            j = i
            while j < n and env[j] > thr:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    gap, short = int(round(min_sil * ENV_RATE)), int(round(min_speech * ENV_RATE))
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= short]


def emphasis_points(env, spacing=1.5):
    """Momenti di enfasi: picchi della voce (media su 300 ms) sopra il suo 85° percentile."""
    n, w = len(env), 15
    if n < 3 * w:
        return []
    sm, acc = [], 0.0
    pref = [0.0]
    for v in env:
        acc += max(v, -80.0)
        pref.append(acc)
    for i in range(n):
        lo, hi = max(0, i - w), min(n, i + w + 1)
        sm.append((pref[hi] - pref[lo]) / (hi - lo))
    voiced = sorted(x for x in sm if x > -60)
    if not voiced:
        return []
    thr = voiced[int(len(voiced) * 0.85)]
    pts, last = [], -1e9
    for i in range(1, n - 1):
        if sm[i] >= thr and sm[i] >= sm[i - 1] and sm[i] > sm[i + 1] and (i - last) / ENV_RATE >= spacing:
            pts.append(i)
            last = i
    return pts


# ---------------------------------------------------------------- pezzi di clip
_FPS_CACHE = {}


def clip_fps(mpi, fps_tl):
    """Frame rate della clip (i fotogrammi sorgente si contano con questo)."""
    try:
        key = mpi.GetUniqueId()
    except Exception:
        key = id(mpi)
    if key not in _FPS_CACHE:
        try:
            f = norm_fps(mpi.GetClipProperty("FPS"))
        except Exception:
            f = 0
        _FPS_CACHE[key] = f if f > 1 else fps_tl
    return _FPS_CACHE[key]


def src_len(frames, k):
    """Fotogrammi sorgente per 'frames' fotogrammi di timeline (k = fps clip / fps timeline).
    Resolve arrotonda per difetto la durata in timeline: qui si arrotonda per eccesso."""
    return max(1, int(math.ceil(frames * k - 1e-6)))


def piece_of(it, x, D, fps, ti, mtype):
    """Il tratto [x, x+D) di timeline preso dalla sorgente della clip 'it', nei fotogrammi
    della CLIP (che puo' avere un frame rate diverso dalla timeline)."""
    mpi = it.GetMediaPoolItem()
    s0 = it.GetSourceStartFrame()
    if mpi is None or s0 is None:
        return None
    k = clip_fps(mpi, fps) / fps
    sf = int(round(s0 + (x - it.GetStart()) * k))
    info = {"mediaPoolItem": mpi, "startFrame": sf, "endFrame": sf + src_len(D, k), "trackIndex": ti, "_k": k}
    if mtype:
        info["mediaType"] = mtype
    return info


TRANSFORM_KEYS = ("Pan", "Tilt", "ZoomX", "ZoomY", "ZoomGang", "RotationAngle", "AnchorPointX", "AnchorPointY",
                  "Pitch", "Yaw", "FlipX", "FlipY", "CropLeft", "CropRight", "CropTop", "CropBottom",
                  "CropSoftness", "CropRetain", "Opacity")


def copy_look(src_it, dst_it):
    """La copia di un pezzo resta come l'originale: grade e trasformazioni."""
    try:
        src_it.CopyGrades([dst_it])
    except Exception:
        pass
    try:
        props = src_it.GetProperty() or {}
        for k in TRANSFORM_KEYS:
            if k in props:
                dst_it.SetProperty(k, props[k])
    except Exception:
        pass


def new_timeline(mp, name):
    tl = mp.CreateEmptyTimeline(name)
    k = 2
    while tl is None and k < 100:
        tl = mp.CreateEmptyTimeline("%s (%d)" % (name, k))
        k += 1
    return tl


def rebuild(src_tl, segs, name, keep_gaps=False, jl=(0, 0), progress=None):
    """Timeline NUOVA con i tratti 'segs' della timeline sorgente, tutte le tracce video e
    audio allineate al fotogramma (anche clip a frame rate diverso dalla timeline).
    segs: [(a, b, {"audio": bool, "color": str|None, "marker": (nome, nota)|None})].
    keep_gaps: ogni tratto resta al suo posto (spazi vuoti al posto del resto).
    jl: (anticipo, ritardo) dell'audio in fotogrammi (taglio J / taglio L), su tracce
    audio alternate per poterlo sovrapporre."""
    pj = current_project()
    mp = pj.GetMediaPool()
    fps = tl_fps(src_tl)
    nv, na = src_tl.GetTrackCount("video") or 0, src_tl.GetTrackCount("audio") or 0
    vitems = [(it, ti) for ti in range(1, nv + 1) for it in (src_tl.GetItemListInTrack("video", ti) or [])]
    aitems = [(it, ti) for ti in range(1, na + 1) for it in (src_tl.GetItemListInTrack("audio", ti) or [])]
    if not vitems and not aitems:
        return None, "La timeline non ha clip."
    t0 = src_tl.GetStartFrame()
    new_tl = new_timeline(mp, name)
    if new_tl is None:
        return None, "Impossibile creare la timeline nuova."
    pj.SetCurrentTimeline(new_tl)
    use_jl = (jl[0] or jl[1]) and not keep_gaps and na
    try:
        while (new_tl.GetTrackCount("video") or 1) < max(1, nv):
            new_tl.AddTrack("video")
        need_a = na * (2 if use_jl else 1)
        while (new_tl.GetTrackCount("audio") or 1) < need_a:
            new_tl.AddTrack("audio")
        for i in range(1, nv + 1):
            new_tl.SetTrackName("video", i, src_tl.GetTrackName("video", i))
        for i in range(1, na + 1):
            new_tl.SetTrackName("audio", i, src_tl.GetTrackName("audio", i))
    except Exception:
        pass
    ns = new_tl.GetStartFrame()
    rebuild_audio = bool(aitems)
    bounds = set()
    for it, _ in vitems + aitems:
        bounds.add(it.GetStart())
        bounds.add(it.GetEnd())
    cursor, placed, fails = 0, 0, 0
    for n, (a, b, opt) in enumerate(segs):
        if progress and n % 10 == 0:
            progress("pezzo %d di %d" % (n + 1, len(segs)))
        if b <= a:
            continue
        pts = sorted({a, b} | {x for x in bounds if a < x < b})
        first = None
        for x, y in zip(pts, pts[1:]):
            D = y - x
            vp = [(ti, piece_of(it, x, D, fps, ti, 1 if rebuild_audio else 0))
                  for it, ti in vitems if it.GetStart() <= x and it.GetEnd() >= y]
            vp = sorted([p for p in vp if p[1]], key=lambda p: p[0])
            ap = []
            if rebuild_audio and opt.get("audio", True):
                ap = [(it, ti, piece_of(it, x, D, fps, ti, 2)) for it, ti in aitems
                      if it.GetStart() <= x and it.GetEnd() >= y]
                ap = [p for p in ap if p[2]]
            if not vp and not ap:
                continue
            rec = ns + (x - t0) if keep_gaps else ns + cursor
            if vp:                                # ancora: video della traccia piu' bassa
                anchor = vp[0][1]
                anchor.pop("_k", None)
                if keep_gaps:
                    anchor["recordFrame"] = rec
                try:
                    got = mp.AppendToTimeline([anchor])
                except Exception:
                    got = None
                if not got:
                    fails += 1
                    continue
                try:
                    rec, dur = got[0].GetStart(), got[0].GetDuration()
                except Exception:
                    dur = D
                made = [got[0]]
                others = [v for _, v in vp[1:]]
            else:                                 # solo audio in questo tratto
                dur = D
                made, others = [], []
            for info in others:
                info["recordFrame"] = rec
                info["endFrame"] = info["startFrame"] + src_len(dur, info.pop("_k"))
                try:
                    g = mp.AppendToTimeline([info])
                    made += g or []
                except Exception:
                    pass
            for it, ti, info in ap:
                k = info.pop("_k")
                pre = jl[0] if (use_jl and x == a and n > 0) else 0
                post = jl[1] if (use_jl and y == b and n < len(segs) - 1) else 0
                pre = min(pre, max(0, x - it.GetStart()))
                post = min(post, max(0, it.GetEnd() - y))
                info["startFrame"] -= int(round(pre * k))
                info["endFrame"] = info["startFrame"] + src_len(dur + pre + post, k)
                info["recordFrame"] = rec - pre
                if use_jl and n % 2:
                    info["trackIndex"] = ti + na      # pezzi alterni: l'audio si sovrappone
                try:
                    g = mp.AppendToTimeline([info])
                    made += g or []
                except Exception:
                    pass
            if opt.get("color"):
                for m in made:
                    try:
                        m.SetClipColor(opt["color"])
                    except Exception:
                        pass
            if first is None:
                first = rec - ns
            cursor = rec + dur - ns
            placed += 1
        if first is not None and opt.get("marker"):
            try:
                new_tl.AddMarker(int(first), "Yellow", opt["marker"][0], opt["marker"][1], 1, "")
            except Exception:
                pass
    return new_tl, ("" if placed else "Nessun pezzo montato.") + (" %d pezzi non riusciti." % fails if fails else "")


# ---------------------------------------------------------------- playhead
PLAYHEAD = {"seq": 0, "field": "", "tc": ""}


def on_playhead(field):
    """Il timecode della playhead di Resolve va nel campo 'da' o 'a' della finestra."""
    tl = current_timeline()
    if not tl:
        set_status("Nessuna timeline aperta.")
        return
    PLAYHEAD.update(seq=PLAYHEAD["seq"] + 1, field=field if field in ("from", "to") else "from",
                    tc=(tl.GetCurrentTimecode() or "").replace(";", ":"))


def on_goto(tc):
    """Porta la playhead di Resolve su un timecode (pagina Edit)."""
    tl = current_timeline()
    if not tl or not tc:
        return
    try:
        if resolve.GetCurrentPage() not in ("edit", "cut"):
            resolve.OpenPage("edit")
    except Exception:
        pass
    tl.SetCurrentTimecode(str(tc))

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

PAGE_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>PlutoniCamera</title>
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
/* ---- pezzi d'interfaccia comuni agli strumenti sulla timeline */
.grid { max-width: 1180px; }
.top { max-width: 1180px; }
.src { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.src .tile { flex: 1; min-width: 220px; }
.src .v.song { font-size: 17px; }
.tchips { display: flex; flex-wrap: wrap; gap: 6px; }
.tchip { all: unset; cursor: pointer; padding: 7px 12px; border-radius: calc(var(--r) - 6px); background: var(--seg); border: var(--bd2);
  min-width: 64px; text-align: center; transition: background .15s; }
.tchip b { display: block; font-size: 13px; }
.tchip small { display: block; font-size: 11px; color: var(--muted); }
.tchip:hover { background: var(--seg-h); }
.tchip.on { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.tchip.on small { color: inherit; opacity: .8; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.cols3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.cols > div, .cols3 > div { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rng { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.rng input, .num input, input.txt { font: 600 14px ui-monospace, Consolas, monospace; color: var(--fg); background: var(--seg);
  border: var(--bd2); border-radius: calc(var(--r) - 6px); padding: 8px 10px; outline: none; width: 130px; }
.rng .mini, .mini { all: unset; cursor: pointer; padding: 7px 10px; border-radius: calc(var(--r) - 6px); background: var(--btn); border: var(--bd2); font-size: 13px; }
.mini:hover { background: var(--btn-h); }
.num { display: flex; align-items: center; gap: 8px; }
.num input { width: 84px; }
.num span { font-size: 12px; color: var(--muted); }
.slider { display: flex; align-items: center; gap: 12px; }
.slider input[type=range] { flex: 1; accent-color: var(--accent); }
.slider output { font: 700 15px ui-monospace, Consolas, monospace; min-width: 72px; text-align: right; }
.opt { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
.opt .t b { display: block; font-size: 13.5px; }
.opt .t small { display: block; font-size: 11.5px; color: var(--muted); }
.stats { display: flex; flex-wrap: wrap; gap: 6px; }
.prev { position: relative; height: 170px; border: var(--bd2); border-radius: calc(var(--r) - 6px); background: var(--tile); overflow: hidden; }
.prev canvas { position: absolute; inset: 0; width: 100%; height: 100%; cursor: crosshair; }
.tip2 { position: absolute; pointer-events: none; font: 600 11.5px ui-monospace, Consolas, monospace; padding: 4px 7px; border-radius: 6px;
  background: rgba(0,0,0,.75); color: #fff; white-space: nowrap; display: none; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 11px; height: 11px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
.btn.cta { font-size: 20px; padding: 18px; }
@media (max-width: 860px) { .cols, .cols3 { grid-template-columns: 1fr; } }
.rng[hidden], .hint[hidden] { display: none !important; }

.camrows { display: flex; flex-direction: column; gap: 8px; }
.camrow { display: grid; grid-template-columns: 22px 1.2fr 1.6fr 1.2fr 110px; gap: 12px; align-items: center; padding: 10px 12px;
  background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 6px); }
.camrow .sw { width: 16px; height: 16px; border-radius: 5px; }
.camrow b { font-size: 14px; }
.camrow small { display: block; color: var(--muted); font-size: 11.5px; }
.stars { display: flex; gap: 4px; }
.stars button { all: unset; cursor: pointer; width: 26px; height: 26px; border-radius: 7px; display: grid; place-items: center;
  font-size: 12px; font-weight: 800; background: var(--seg); border: var(--bd2); }
.stars button.on { background: var(--accent-bg); color: var(--onaccent); }
.camrow select { font: inherit; color: var(--fg); background: var(--seg); border: var(--bd2); border-radius: 8px; padding: 6px 8px; width: 100%; }
.camrow select option { background: var(--card); color: var(--fg); }
.camrow label.first { display: flex; align-items: center; gap: 6px; font-size: 12px; cursor: pointer; }
.trigs { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.trig { padding: 12px; border: var(--bd2); border-radius: calc(var(--r) - 6px); background: var(--tile); display: flex; flex-direction: column; gap: 8px; }
.prev { height: 120px; }
.share { display: flex; flex-wrap: wrap; gap: 8px; }
.row2 { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
@media (max-width: 960px) { .camrow { grid-template-columns: 22px 1fr 1fr; } .trigs { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body class="look-studio" data-look="studio">
<div class="bgfx"></div>
<header class="top">
  <img id="logo" src="/logo" alt="">
  <div class="brand"><div class="name">PlutoniCamera</div><div class="tag">cambi camera automatici · Plutonic</div></div>
  <div class="spacer"></div>
  <button class="pin" id="pin" title="Tiene la finestra davanti a Resolve">📌 Sempre davanti</button>
  <div class="looks"><span>Aspetto</span><button data-look="studio">Studio</button><button data-look="labs">Labs</button><button data-look="notte">Notte</button></div>
</header>
<main class="grid">
  <section class="card wide">
    <h2><span class="n">1</span> Timeline e camere</h2>
    <div class="src">
      <div class="tile"><div class="k">Timeline aperta in Resolve</div><div class="v song" id="tlName">—</div><div class="sub" id="tlInfo">—</div></div>
      <button class="btn" id="bRefresh">↻ Aggiorna</button>
    </div>
    <label>Tracce camera (una per angolo, già in sync)</label>
    <div class="tchips" id="cams"></div>
    <div class="camrows" id="camrows"></div>
    <div class="hint">Priorità: quanto spesso viene scelta quella camera (es. il campo largo più spesso dei primi piani).
      Microfono: se ogni persona ha il suo, PlutoniCamera può inquadrare chi parla.</div>
  </section>
  <section class="card wide">
    <h2><span class="n">2</span> Quando cambiare camera</h2>
    <div class="trigs">
      <div class="trig"><label class="opt"><button class="switch" id="t_pause"><i></i></button><span class="t"><b>Pause</b><small>quando ricomincia a parlare</small></span></label>
        <div class="num"><input id="pause_min" type="number" step="0.1" min="0.2"><span>s di pausa minima</span></div></div>
      <div class="trig"><label class="opt"><button class="switch" id="t_speaker"><i></i></button><span class="t"><b>Chi parla</b><small>serve il microfono delle camere</small></span></label></div>
      <div class="trig"><label class="opt"><button class="switch" id="t_emph"><i></i></button><span class="t"><b>Enfasi</b><small>quando la voce si alza</small></span></label></div>
      <div class="trig"><label class="opt"><button class="switch" id="t_cuts"><i></i></button><span class="t"><b>Tagli esistenti</b><small>dove le clip camera cambiano</small></span></label></div>
    </div>
    <div class="cols3">
      <div><label>Voce da ascoltare (pause ed enfasi)</label><div class="tchips" id="voice"></div></div>
      <div><label>Durata minima inquadratura</label><div class="num"><input id="min_shot" type="number" step="0.5" min="0.5"><span>s — niente cambi più ravvicinati</span></div></div>
      <div><label>Durata massima inquadratura</label><div class="num"><input id="max_shot" type="number" step="1" min="1"><span>s — poi si cambia comunque</span></div></div>
    </div>
    <div class="cols">
      <div><label>Parte da montare</label>
        <div class="seg"><button id="rAll"><b>Tutta la timeline</b></button><button id="rSel"><b>Solo un tratto</b><small>da … a …</small></button></div>
        <div class="rng" id="rBox" hidden>da <input id="from" placeholder="01:00:00:00"><button class="mini" id="ph_from" title="Prendi la playhead di Resolve">◎</button>
          a <input id="to" placeholder="01:05:00:00"><button class="mini" id="ph_to" title="Prendi la playhead di Resolve">◎</button></div></div>
      <div><label>&nbsp;</label><div class="hint" style="margin:0">Regola fissa: la stessa camera non va mai in onda due volte di fila.</div></div>
    </div>
    <button class="btn primary" id="bAnalyze">Analizza e prepara i cambi</button>
    <div class="hint" id="ffw" hidden>⚠ ffmpeg non trovato: senza, si cambia solo sui tagli esistenti e a durata massima.</div>
  </section>
  <section class="card wide">
    <div class="row2"><h2 style="margin:0">Anteprima dei cambi</h2><span class="spacer" style="flex:1"></span>
      <span class="hint" style="margin:0">clic = porta lì la playhead di Resolve</span>
      <button class="btn ghost" id="bShuffle" title="Un'altra scelta delle camere con le stesse regole">🎲 Rimescola</button></div>
    <div class="prev" id="prevBox"><canvas id="cv"></canvas><div class="tip2" id="tip"></div></div>
    <div class="share" id="share"></div>
  </section>
  <section class="card wide">
    <h2><span class="n">3</span> Monta</h2>
    <div class="cols">
      <label class="opt"><button class="switch" id="disable_src"><i></i></button><span class="t"><b>Spegni le tracce camera originali</b>
        <small>i cambi vanno su una traccia nuova in cima (solo se elabori tutta la timeline)</small></span></label>
      <label class="opt"><button class="switch" id="copy"><i></i></button><span class="t"><b>Lavora su una copia della timeline</b>
        <small>l'originale resta com'è</small></span></label>
    </div>
    <div class="row2"><button class="btn cta" id="bApply" style="flex:1">🎥 Monta i cambi camera</button>
      <button class="btn" id="bUndo" title="Toglie la traccia PlutoniCamera e riaccende le camere">↺ Togli i cambi</button></div>
    <div class="hint" style="margin:0">Tocca solo il video: l'audio della timeline resta esattamente com'è.</div>
  </section>
  <section class="card wide">
    <div class="status"><span class="dot"></span><span id="status">Pronto.</span><span class="spacer"></span>
      <button class="btn ghost" id="bQuit">Chiudi PlutoniCamera</button></div>
    <pre class="log" id="log"></pre>
  </section>
</main>
<div id="off" class="overlay" hidden><div class="card"><h2>PlutoniCamera è chiuso</h2><p>Riaprilo da PlutoniCut o da Workspace ▸ Scripts ▸ Utility.</p></div></div>
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


// ---- pezzi d'interfaccia comuni agli strumenti che lavorano sulla timeline aperta
const fmtS = s => { s = Math.max(0, s); const m = Math.floor(s / 60), x = s - m * 60;
  return m + ":" + (x < 10 ? "0" : "") + x.toFixed(1).replace(".", ","); };
function seg2(id, opts, val, onPick) {        // pulsanti a scelta singola: opts = [[valore, titolo, sotto]]
  const el = $(id);
  el.innerHTML = opts.map(o => "<button data-v='" + o[0] + "'><b>" + esc(o[1]) + "</b>" +
    (o[2] ? "<small>" + esc(o[2]) + "</small>" : "") + "</button>").join("");
  for (const b of el.children) b.onclick = () => { pick2(id, b.dataset.v); if (onPick) onPick(b.dataset.v); push(); };
  pick2(id, val);
}
function pick2(id, v) { for (const b of $(id).children) b.classList.toggle("on", b.dataset.v === String(v)); }
const val2 = id => { const b = $(id).querySelector(".on"); return b ? b.dataset.v : ""; };
function chips(id, list, sel, single) {        // tracce da scegliere: list = [{i, name, n}]
  const el = $(id);
  el.innerHTML = list.length ? list.map(t => "<button class='tchip" + (sel.includes(t.i) ? " on" : "") + "' data-i='" + t.i + "' title='" +
    esc(t.name) + " · " + t.n + " clip'><b>" + esc(t.name) + "</b><small>" + t.n + " clip</small></button>").join("")
    : "<span class='hint' style='margin:0'>nessuna traccia</span>";
  for (const b of el.querySelectorAll(".tchip")) b.onclick = () => {
    if (single) for (const x of el.children) x.classList.toggle("on", x === b); else b.classList.toggle("on");
    push(); if (window.onChips) onChips(id);
  };
}
const chosen = id => [...$(id).querySelectorAll(".tchip.on")].map(b => +b.dataset.i);
function sectionUI() {                         // tutta la timeline / da-a con la playhead
  $("rAll").onclick = () => { $("rAll").classList.add("on"); $("rSel").classList.remove("on"); $("rBox").hidden = true; push(); };
  $("rSel").onclick = () => { $("rSel").classList.add("on"); $("rAll").classList.remove("on"); $("rBox").hidden = false; push(); };
  for (const f of ["from", "to"]) {
    $(f).addEventListener("change", () => push());
    $("ph_" + f).onclick = () => cmd("playhead", f, true);
  }
}
let seenPh = -1;
function sectionSync(s) {
  $("rAll").classList.toggle("on", !s.range); $("rSel").classList.toggle("on", !!s.range); $("rBox").hidden = !s.range;
  $("from").value = s.from || ""; $("to").value = s.to || "";
}
function playheadIn() {                        // il timecode della playhead e' arrivato
  const ph = ST.playhead;
  if (!ph || ph.seq === seenPh) return;
  if (seenPh >= 0 && ph.tc) { $(ph.field).value = ph.tc; push(); }
  seenPh = ph.seq;
}
const sectionGet = () => ({range: $("rSel").classList.contains("on"), from: $("from").value.trim(), to: $("to").value.trim()});

const BUSY = {refresh: "Leggo la timeline…", analyze: "Ascolto la voce e leggo le camere…", apply: "Monto i cambi camera…",
  undo: "Tolgo i cambi…", quit: "Chiudo…"};
const BTN = {refresh: "bRefresh", analyze: "bAnalyze", apply: "bApply", undo: "bUndo", quit: "bQuit"};
const COLORS = ["#8b5cf6", "#f59e0b", "#22c55e", "#0ea5e9", "#ef4444", "#ec4899", "#eab308", "#14b8a6"];
const SW = ["t_pause", "t_speaker", "t_emph", "t_cuts", "disable_src", "copy"];
let P = null, prio = {}, mic = {}, first = 0, planT = null, planAt = 0;
const on = id => $(id).classList.contains("on");
const color = c => COLORS[(ST.src.tracks.video.findIndex(t => t.i === c)) % COLORS.length];
const vname = c => (ST.src.tracks.video.find(t => t.i === c) || {name: "V" + c}).name;
function collect() {
  clearTimeout(planT); planT = setTimeout(loadPlan, 350);
  const o = Object.assign(sectionGet(), {cams: chosen("cams"), voice: chosen("voice"), prio: prio, mic: mic, first: first,
    pause_min: +$("pause_min").value, min_shot: +$("min_shot").value, max_shot: +$("max_shot").value});
  for (const k of SW) o[k] = on(k);
  return o;
}
function camRows() {
  const cams = chosen("cams"), au = ST.src.tracks.audio;
  $("camrows").innerHTML = cams.map(c => "<div class='camrow' data-c='" + c + "'><span class='sw' style='background:" + color(c) + "'></span>" +
    "<div><b>" + esc(vname(c)) + "</b><small>V" + c + "</small></div>" +
    "<div class='stars' title='Priorità'>" + [1, 2, 3, 4, 5].map(k => "<button data-k='" + k + "'" + ((prio[c] || 3) >= k ? " class='on'" : "") + ">" + k + "</button>").join("") + "</div>" +
    "<select title='Microfono della persona inquadrata'><option value='0'>— nessun microfono</option>" +
    au.map(t => "<option value='" + t.i + "'" + ((mic[c] || 0) == t.i ? " selected" : "") + ">🎤 " + esc(t.name) + "</option>").join("") + "</select>" +
    "<label class='first'><input type='radio' name='first'" + (first === c ? " checked" : "") + "> si parte da qui</label></div>").join("");
  for (const row of $("camrows").children) {
    const c = +row.dataset.c;
    for (const b of row.querySelectorAll(".stars button")) b.onclick = () => { prio[c] = +b.dataset.k; camRows(); push(); };
    row.querySelector("select").onchange = e => { mic[c] = +e.target.value; push(); };
    row.querySelector("input[type=radio]").onchange = () => { first = c; push(); };
  }
}
window.onChips = id => { if (id === "cams") camRows(); };
async function loadPlan() {
  try { P = await (await fetch("/api/plan", {cache: "no-store"})).json(); planAt = ST ? ST.anver : 0; draw(); } catch (e) {}
}
const cv = $("cv");
function draw() {
  const W = cv.clientWidth, H = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const cs = getComputedStyle(document.body);
  if (!P || !P.segs.length) {
    g.fillStyle = cs.getPropertyValue("--muted"); g.font = "13px sans-serif"; g.textAlign = "center";
    g.fillText("Scegli le camere e premi «Analizza e prepara i cambi».", W / 2, H / 2); $("share").innerHTML = ""; return;
  }
  const X = f => (f - P.a) / (P.b - P.a) * W, tot = {};
  for (const [s, e, c] of P.segs) {
    g.fillStyle = c ? color(c) : "rgba(128,128,128,.25)";
    g.fillRect(X(s), 22, Math.max(1, X(e) - X(s) - 1), H - 44);
    if (c) tot[c] = (tot[c] || 0) + e - s;
  }
  if (P.voice.length) {                         // la voce, piccola, sopra
    g.fillStyle = cs.getPropertyValue("--muted");
    for (let x = 0; x < W; x++) { const i = Math.floor(x / W * P.voice.length), v = Math.max(0, (P.voice[i] + 60) / 60);
      g.fillRect(x, 18 - v * 16, 1, v * 16); }
  }
  const tc = {taglio: "#94a3b8", pausa: "#f59e0b", enfasi: "#ef4444", parla: "#22c55e"};
  for (const [f, k] of P.trig) { g.fillStyle = tc[k] || "#fff"; g.fillRect(X(f), H - 18, 1.5, 12); }
  const all = Object.values(tot).reduce((a, b) => a + b, 0) || 1;
  $("share").innerHTML = "<span class='chip'><b>" + P.segs.filter(s => s[2]).length + "</b> inquadrature</span>" +
    Object.entries(tot).map(([c, v]) => "<span class='chip'><i style='display:inline-block;width:10px;height:10px;border-radius:3px;background:" +
      color(+c) + "'></i> " + esc(vname(+c)) + " <b>" + Math.round(100 * v / all) + "%</b></span>").join("") +
    "<span class='chip'>tacche: <b style='color:#f59e0b'>pausa</b> · <b style='color:#22c55e'>chi parla</b> · <b style='color:#ef4444'>enfasi</b> · <b style='color:#94a3b8'>taglio</b></span>";
}
const fAt = px => P.a + px / cv.clientWidth * (P.b - P.a);
const tcOf = f => { const fi = Math.round(P.fps); f = Math.round(f);
  return [Math.floor(f / (fi * 3600)), Math.floor(f / (fi * 60)) % 60, Math.floor(f / fi) % 60, f % fi].map(x => String(x).padStart(2, "0")).join(":"); };
cv.addEventListener("pointermove", e => { if (!P || !P.segs.length) return; const f = fAt(e.offsetX), sg = P.segs.find(s => s[0] <= f && f < s[1]), t = $("tip");
  t.style.display = "block"; t.style.left = Math.min(e.offsetX + 12, cv.clientWidth - 170) + "px"; t.style.top = "26px";
  t.textContent = tcOf(f) + " · " + (sg && sg[2] ? vname(sg[2]) : "nessuna camera"); });
cv.addEventListener("pointerleave", () => { $("tip").style.display = "none"; });
cv.addEventListener("click", e => { if (P && P.segs.length) cmd("goto", tcOf(fAt(e.offsetX)), true); });
window.addEventListener("resize", draw);
$("bShuffle").onclick = () => { cmd("shuffle", null, true); setTimeout(loadPlan, 400); };
function init() {
  const s = ST.set;
  sectionUI(); sectionSync(s);
  prio = Object.fromEntries(Object.entries(s.prio || {}).map(([k, v]) => [+k, v]));
  mic = Object.fromEntries(Object.entries(s.mic || {}).map(([k, v]) => [+k, v]));
  first = s.first || 0;
  for (const k of SW) { $(k).classList.toggle("on", !!s[k]); $(k).onclick = e => { e.preventDefault(); $(k).classList.toggle("on"); push(); }; }
  for (const k of ["pause_min", "min_shot", "max_shot"]) { $(k).value = s[k]; $(k).addEventListener("change", () => push()); }
  seenPh = ST.playhead.seq;
}
let lastTr = "";
function render() {
  const s = ST.src;
  $("tlName").textContent = s.ok ? s.name : "nessuna timeline aperta";
  $("tlInfo").textContent = s.ok ? s.tracks.video.length + " tracce video · " + s.tracks.audio.length + " tracce audio" +
    (s.done ? " · ci sono già cambi PlutoniCamera" : "") : "apri una timeline in Resolve";
  const sig = JSON.stringify(s.tracks) + ST.set.cams + "|" + ST.set.voice;
  if (sig !== lastTr) { lastTr = sig; chips("cams", s.tracks.video.filter(t => t.name !== "PlutoniCamera"), ST.set.cams);
    chips("voice", s.tracks.audio, ST.set.voice); camRows(); }
  $("ffw").hidden = ST.ffmpeg;
  $("bAnalyze").textContent = ST.fresh ? "↻ Analizza di nuovo" : "Analizza e prepara i cambi";
  if (ST.anver !== planAt) loadPlan();
  if (!ST.busy) $("bUndo").disabled = !s.done;
  playheadIn();
}
start();
</script>
</body>
</html>
"""


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

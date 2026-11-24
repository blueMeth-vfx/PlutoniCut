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

PAGE_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>NoSilence</title>
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

.nums4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.nums4 > div { display: flex; flex-direction: column; gap: 6px; }
.prevbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.prevbar .spacer { flex: 1; }
@media (max-width: 860px) { .nums4 { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body class="look-studio" data-look="studio">
<div class="bgfx"></div>
<header class="top">
  <img id="logo" src="/logo" alt="">
  <div class="brand"><div class="name">NoSilence</div><div class="tag">togli i silenzi tra le parole · Plutonic</div></div>
  <div class="spacer"></div>
  <button class="pin" id="pin" title="Tiene la finestra davanti a Resolve">📌 Sempre davanti</button>
  <div class="looks"><span>Aspetto</span><button data-look="studio">Studio</button><button data-look="labs">Labs</button><button data-look="notte">Notte</button></div>
</header>
<main class="grid">
  <section class="card wide">
    <h2><span class="n">1</span> Timeline e tracce</h2>
    <div class="src">
      <div class="tile"><div class="k">Timeline aperta in Resolve</div><div class="v song" id="tlName">—</div><div class="sub" id="tlInfo">—</div></div>
      <button class="btn" id="bRefresh">↻ Aggiorna</button>
    </div>
    <div class="cols">
      <div><label>Tracce audio da ascoltare (la voce)</label><div class="tchips" id="tracks"></div></div>
      <div><label>Parte da ripulire</label>
        <div class="seg"><button id="rAll"><b>Tutta la timeline</b></button><button id="rSel"><b>Solo un tratto</b><small>da … a …</small></button></div>
        <div class="rng" id="rBox" hidden>da <input id="from" placeholder="01:00:00:00"><button class="mini" id="ph_from" title="Prendi la playhead di Resolve">◎</button>
          a <input id="to" placeholder="01:05:00:00"><button class="mini" id="ph_to" title="Prendi la playhead di Resolve">◎</button></div></div>
    </div>
    <button class="btn primary" id="bAnalyze">Analizza l'audio</button>
    <div class="hint" id="ffw" hidden>⚠ ffmpeg non trovato: serve per ascoltare l'audio (mettilo in C:\ffmpeg\bin).</div>
  </section>
  <section class="card wide">
    <h2><span class="n">2</span> Soglia e ritmo</h2>
    <label>Soglia del rumore — sotto questo volume è silenzio</label>
    <div class="slider"><input type="range" id="thr" min="-70" max="-10" step="1"><output id="thrV"></output>
      <button class="btn" id="bAuto" title="Stima la soglia dal rumore di fondo e dal livello della voce">✨ Trova da sola</button></div>
    <label>Ritmo</label>
    <div class="seg" id="pace"></div>
    <div class="nums4">
      <div><label>Togli silenzi più lunghi di</label><div class="num"><input id="min_sil" type="number" step="0.05" min="0.05"><span>s</span></div></div>
      <div><label>Tieni parlato più lungo di</label><div class="num"><input id="min_speech" type="number" step="0.05" min="0"><span>s</span></div></div>
      <div><label>Margine prima della voce</label><div class="num"><input id="pad_b" type="number" step="0.01" min="0"><span>s</span></div></div>
      <div><label>Margine dopo la voce</label><div class="num"><input id="pad_a" type="number" step="0.01" min="0"><span>s</span></div></div>
    </div>
  </section>
  <section class="card wide">
    <div class="prevbar"><h2 style="margin:0">Anteprima</h2><span class="spacer"></span>
      <span class="hint" style="margin:0">rotella = zoom · trascina = scorri · clic = porta lì la playhead di Resolve</span>
      <button class="mini" id="zAll">Tutto</button></div>
    <div class="prev" id="prevBox"><canvas id="cv"></canvas><div class="tip2" id="tip"></div></div>
    <div class="legend"><span><i style="background:#ef4444"></i>silenzio tolto</span><span><i style="background:#22c55e"></i>margine tenuto</span>
      <span><i style="background:var(--accent)"></i>voce</span><span><i style="background:#f59e0b"></i>soglia</span></div>
    <div class="stats" id="stats"></div>
  </section>
  <section class="card wide">
    <h2><span class="n">3</span> Cosa fare dei silenzi</h2>
    <div class="seg" id="mode"></div>
    <div class="cols">
      <div><label>Transizione sui tagli</label><div class="seg" id="trans"></div></div>
      <div><label>Sovrapposizione J / L</label><div class="num"><input id="overlap" type="number" step="0.01" min="0.02"><span>s (l'audio entra prima / esce dopo)</span></div></div>
    </div>
    <div class="cols">
      <div><label>Nome della timeline nuova</label><input class="txt" id="tlname" style="width:100%" placeholder="automatico: <timeline>_NoSilence"></div>
      <div><label>&nbsp;</label><div class="hint" style="margin:0">L'originale non viene toccata: NoSilence crea una timeline nuova, video e audio tagliati insieme.</div></div>
    </div>
    <button class="btn cta" id="bApply">✂ Togli i silenzi</button>
  </section>
  <section class="card wide">
    <div class="status"><span class="dot"></span><span id="status">Pronto.</span><span class="spacer"></span>
      <button class="btn ghost" id="bQuit">Chiudi NoSilence</button></div>
    <pre class="log" id="log"></pre>
  </section>
</main>
<div id="off" class="overlay" hidden><div class="card"><h2>NoSilence è chiuso</h2><p>Riaprilo da PlutoniCut o da Workspace ▸ Scripts ▸ Utility.</p></div></div>
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

const BUSY = {refresh: "Leggo la timeline…", analyze: "Ascolto l'audio delle tracce scelte…", auto: "Cerco la soglia…",
  apply: "Monto la timeline senza silenzi…", quit: "Chiudo…"};
const BTN = {refresh: "bRefresh", analyze: "bAnalyze", auto: "bAuto", apply: "bApply", quit: "bQuit"};
const NUMS = ["min_sil", "min_speech", "pad_b", "pad_a"];
let E = null, envVer = -1, v0 = 0, v1 = 1, keepR = [], speechR = [];
const collect = () => Object.assign(sectionGet(), {tracks: chosen("tracks"), thr: +$("thr").value, pace: val2("pace"),
  min_sil: +$("min_sil").value, min_speech: +$("min_speech").value, pad_b: +$("pad_b").value, pad_a: +$("pad_a").value,
  mode: val2("mode"), trans: val2("trans"), overlap: +$("overlap").value, tlname: $("tlname").value});

function speech(env, thr, minSil, minSp) {     // = speech_ranges di NoSilence
  const runs = [], n = env.length;
  for (let i = 0; i < n;) { if (env[i] > thr) { let j = i; while (j < n && env[j] > thr) j++; runs.push([i, j]); i = j; } else i++; }
  const gap = Math.round(minSil * E.rate), short = Math.round(minSp * E.rate), m = [];
  for (const r of runs) { if (m.length && r[0] - m[m.length - 1][1] < gap) m[m.length - 1][1] = r[1]; else m.push(r.slice()); }
  return m.filter(r => r[1] - r[0] >= short);
}
function plan() {                              // = keep_ranges di NoSilence
  if (!E) return;
  const s = collect();
  speechR = speech(E.env, s.thr, s.min_sil, s.min_speech);
  const pb = Math.round(s.pad_b * E.rate), pa = Math.round(s.pad_a * E.rate);
  keepR = [];
  for (const [i0, i1] of speechR) {
    const k0 = Math.max(0, i0 - pb), k1 = Math.min(E.env.length, i1 + pa);
    if (keepR.length && k0 <= keepR[keepR.length - 1][1]) keepR[keepR.length - 1][1] = Math.max(keepR[keepR.length - 1][1], k1);
    else keepR.push([k0, k1]);
  }
  const tot = E.env.length / E.rate, kept = keepR.reduce((a, r) => a + r[1] - r[0], 0) / E.rate;
  $("stats").innerHTML = ["<b>" + Math.max(0, keepR.length - 1) + "</b> tagli", "tolti <b>" + fmtS(tot - kept) + "</b> (" +
    Math.round(100 * (tot - kept) / Math.max(tot, 0.01)) + "%)", "durata <b>" + fmtS(tot) + "</b> → <b>" + fmtS(kept) + "</b>"]
    .map(x => "<span class='chip'>" + x + "</span>").join("");
  draw();
}
const cv = $("cv");
function draw() {
  const W = cv.clientWidth, H = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr);
  const cs = getComputedStyle(document.body);
  g.clearRect(0, 0, W, H);
  if (!E || !E.env.length) {
    g.fillStyle = cs.getPropertyValue("--muted"); g.font = "13px sans-serif"; g.textAlign = "center";
    g.fillText(ST && ST.src.ok ? "Premi «Analizza l'audio» per vedere la voce e i silenzi." : "Apri una timeline in Resolve.", W / 2, H / 2);
    return;
  }
  const n = E.env.length, a = v0 * n, b = v1 * n, per = (b - a) / W, y = db => H - (Math.max(-70, Math.min(0, db)) + 70) / 70 * H;
  const X = i => (i - a) / per;
  g.fillStyle = "rgba(239,68,68,.28)";          // silenzi tolti = tutto cio' che non e' tenuto
  let prev = 0;
  for (const [k0, k1] of keepR.concat([[n, n]])) { if (k0 > prev) g.fillRect(X(prev), 0, X(k0) - X(prev), H); prev = k1; }
  g.fillStyle = "rgba(34,197,94,.22)";           // margini: tenuti ma sotto soglia
  for (const [k0, k1] of keepR) {
    const sp = speechR.filter(r => r[1] > k0 && r[0] < k1);
    let p = k0;
    for (const [s0, s1] of sp) { if (s0 > p) g.fillRect(X(p), 0, X(s0) - X(p), H); p = Math.max(p, s1); }
    if (k1 > p) g.fillRect(X(p), 0, X(k1) - X(p), H);
  }
  g.fillStyle = cs.getPropertyValue("--accent").trim();
  for (let x = 0; x < W; x++) {                 // livello: massimo per pixel
    const i0 = Math.floor(a + x * per), i1 = Math.max(i0 + 1, Math.floor(a + (x + 1) * per));
    let m = -99; for (let i = i0; i < i1 && i < n; i++) if (E.env[i] > m) m = E.env[i];
    const yy = y(m); g.fillRect(x, yy, 1, H - yy);
  }
  const ty = y(+$("thr").value);
  g.strokeStyle = "#f59e0b"; g.lineWidth = 2; g.setLineDash([6, 4]);
  g.beginPath(); g.moveTo(0, ty); g.lineTo(W, ty); g.stroke(); g.setLineDash([]);
}
const idxAt = px => (v0 + (v1 - v0) * px / cv.clientWidth) * E.env.length;
const tcAt = i => { const f = E.a + Math.round(i / E.rate * E.fps), fi = Math.round(E.fps);
  return [Math.floor(f / (fi * 3600)), Math.floor(f / (fi * 60)) % 60, Math.floor(f / fi) % 60, f % fi].map(x => String(x).padStart(2, "0")).join(":"); };
let drag = null;
cv.addEventListener("wheel", e => { if (!E) return; e.preventDefault();
  const f = e.offsetX / cv.clientWidth, c = v0 + (v1 - v0) * f, span = Math.min(1, Math.max(200 / E.env.length, (v1 - v0) * (e.deltaY > 0 ? 1.25 : 0.8)));
  v0 = Math.max(0, c - span * f); v1 = Math.min(1, v0 + span); v0 = Math.max(0, v1 - span); draw(); }, {passive: false});
cv.addEventListener("pointerdown", e => { drag = {x: e.offsetX, v0, v1, moved: false}; cv.setPointerCapture(e.pointerId); });
cv.addEventListener("pointermove", e => {
  if (!E) return;
  if (drag) { const d = (e.offsetX - drag.x) / cv.clientWidth * (drag.v1 - drag.v0);
    if (Math.abs(e.offsetX - drag.x) > 3) drag.moved = true;
    const span = drag.v1 - drag.v0; v0 = Math.max(0, Math.min(1 - span, drag.v0 - d)); v1 = v0 + span; draw(); }
  const i = Math.floor(idxAt(e.offsetX)), t = $("tip");
  if (i >= 0 && i < E.env.length) { t.style.display = "block"; t.style.left = Math.min(e.offsetX + 12, cv.clientWidth - 150) + "px"; t.style.top = "8px";
    t.textContent = tcAt(i) + " · " + E.env[i] + " dB"; }
});
cv.addEventListener("pointerup", e => { if (drag && !drag.moved && E) cmd("goto", tcAt(Math.floor(idxAt(e.offsetX))), true); drag = null; });
cv.addEventListener("pointerleave", () => { $("tip").style.display = "none"; });
$("zAll").onclick = () => { v0 = 0; v1 = 1; draw(); };
window.addEventListener("resize", draw);
async function loadEnv() {
  try { E = await (await fetch("/api/env", {cache: "no-store"})).json(); envVer = E.ver; v0 = 0; v1 = 1; plan(); } catch (e) {}
}
function applyPace(p) {
  const v = ST.paces[p]; if (!v) return;
  NUMS.forEach((k, i) => { $(k).value = v[i]; }); plan();
}
function init() {
  const s = ST.set;
  sectionUI(); sectionSync(s);
  $("thr").value = s.thr; $("thrV").textContent = s.thr + " dB";
  seg2("pace", [["calmo", "Calmo"], ["misurato", "Misurato"], ["cadenzato", "Cadenzato"], ["energico", "Energico"], ["turbo", "Turbo"],
    ["custom", "A mano"]], s.pace, p => applyPace(p));
  NUMS.forEach(k => { $(k).value = s[k]; $(k).addEventListener("input", () => { pick2("pace", "custom"); plan(); });
    $(k).addEventListener("change", () => push()); });
  seg2("mode", [["remove", "Rimuovi", "toglie i silenzi"], ["keep", "Taglia e lascia", "pezzi separati, silenzi arancioni"],
    ["mute", "Muta", "video resta, audio tolto"], ["gaps", "Rimuovi e lascia lo spazio", "non ricompatta"]], s.mode);
  seg2("trans", [["none", "Nessuna", "tagli netti"], ["j", "Taglio J", "l'audio entra prima"], ["l", "Taglio L", "l'audio esce dopo"]], s.trans);
  $("overlap").value = s.overlap; $("overlap").addEventListener("change", () => push());
  $("tlname").value = s.tlname || ""; $("tlname").addEventListener("change", () => push());
  $("thr").addEventListener("input", () => { $("thrV").textContent = $("thr").value + " dB"; plan(); });
  $("thr").addEventListener("change", () => push());
  seenPh = ST.playhead.seq;
}
let lastTracks = "";
function render() {
  const s = ST.src;
  $("tlName").textContent = s.ok ? s.name : "nessuna timeline aperta";
  $("tlInfo").textContent = s.ok ? s.tracks.video.length + " tracce video · " + s.tracks.audio.length + " tracce audio · " + (+s.fps).toFixed(3).replace(/\.?0+$/, "") + " fps" : "apri una timeline in Resolve";
  const sig = JSON.stringify(s.tracks.audio) + ST.set.tracks;
  if (sig !== lastTracks) { lastTracks = sig; chips("tracks", s.tracks.audio, ST.set.tracks); }
  $("ffw").hidden = ST.ffmpeg;
  if (ST.envver !== envVer) loadEnv();
  if (Date.now() - lastLocal > 2000 && +$("thr").value !== ST.set.thr) { $("thr").value = ST.set.thr; $("thrV").textContent = ST.set.thr + " dB"; plan(); }
  $("bAnalyze").textContent = ST.fresh ? "↻ Analizza di nuovo" : "Analizza l'audio";
  if (!ST.busy) $("bApply").disabled = !s.ok;
  playheadIn();
}
start();
</script>
</body>
</html>
"""


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

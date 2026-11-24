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


def _item_key(it):
    try:
        return it.GetUniqueId()
    except Exception:
        return id(it)


def _link_groups(items):
    """Gruppi di clip collegate (video + il suo audio) nella timeline sorgente."""
    group = {}
    for it, _ in items:
        k = _item_key(it)
        if k in group:
            continue
        group[k] = k
        try:
            linked = it.GetLinkedItems() or []
        except Exception:
            linked = []
        for o in linked:
            group.setdefault(_item_key(o), k)
    return group


def _relink(tl, pairs, group):
    """Ricollega fra loro i pezzi nuovi che vengono da clip collegate nell'originale."""
    by = {}
    for src, new in pairs:
        by.setdefault(group.get(_item_key(src), _item_key(src)), []).append(new)
    for items in by.values():
        if len(items) > 1:
            try:
                tl.SetClipsLinked(items, True)
            except Exception:
                pass


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
    group = _link_groups(vitems + aitems)
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
            vp = [(ti, it, piece_of(it, x, D, fps, ti, 1 if rebuild_audio else 0))
                  for it, ti in vitems if it.GetStart() <= x and it.GetEnd() >= y]
            vp = sorted([p for p in vp if p[2]], key=lambda p: p[0])
            ap = []
            if rebuild_audio and opt.get("audio", True):
                ap = [(it, ti, piece_of(it, x, D, fps, ti, 2)) for it, ti in aitems
                      if it.GetStart() <= x and it.GetEnd() >= y]
                ap = [p for p in ap if p[2]]
            if not vp and not ap:
                continue
            rec = ns + (x - t0) if keep_gaps else ns + cursor
            pairs = []                            # (clip sorgente, pezzo nuovo): per ricollegarli
            if vp:                                # ancora: video della traccia piu' bassa
                anchor = vp[0][2]
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
                pairs.append((vp[0][1], got[0]))
                others = [(src, v) for _, src, v in vp[1:]]
            else:                                 # solo audio in questo tratto
                dur = D
                made, others = [], []
            for src, info in others:
                info["recordFrame"] = rec
                info["endFrame"] = info["startFrame"] + src_len(dur, info.pop("_k"))
                try:
                    g = mp.AppendToTimeline([info]) or []
                    made += g
                    pairs += [(src, m) for m in g]
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
                    g = mp.AppendToTimeline([info]) or []
                    made += g
                    pairs += [(it, m) for m in g]
                except Exception:
                    pass
            _relink(new_tl, pairs, group)
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

PAGE_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>ZoomTonic</title>
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

.anchor { display: grid; grid-template-columns: minmax(0, 1.2fr) 1fr; gap: 16px; align-items: start; }
.frame { position: relative; border-radius: calc(var(--r) - 6px); overflow: hidden; border: var(--bd2); background: #000;
  aspect-ratio: 16 / 9; cursor: crosshair; display: grid; place-items: center; color: #888; font-size: 13px; }
.frame img { width: 100%; height: 100%; object-fit: contain; display: block; }
.frame .x { position: absolute; width: 34px; height: 34px; margin: -17px 0 0 -17px; border: 2px solid #fff; border-radius: 50%;
  box-shadow: 0 0 0 2px rgba(0,0,0,.6); pointer-events: none; }
.frame .x::after { content: ""; position: absolute; left: 50%; top: 50%; width: 6px; height: 6px; margin: -3px; border-radius: 50%; background: #fff; }
.frame .zb { position: absolute; border: 2px dashed rgba(255,255,255,.8); pointer-events: none; }
.styles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.prev { height: 130px; }
.row2 { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
@media (max-width: 860px) { .anchor { grid-template-columns: 1fr; } }
</style>
</head>
<body class="look-studio" data-look="studio">
<div class="bgfx"></div>
<header class="top">
  <img id="logo" src="/logo" alt="">
  <div class="brand"><div class="name">ZoomTonic</div><div class="tag">zoom dinamici automatici · Plutonic</div></div>
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
      <div><label>Traccia video su cui mettere gli zoom</label><div class="tchips" id="track"></div></div>
      <div><label>Voce da ascoltare (inizio frase ed enfasi)</label><div class="tchips" id="voice"></div></div>
    </div>
    <div class="cols">
      <div><label>Parte da animare</label>
        <div class="seg"><button id="rAll"><b>Tutta la timeline</b></button><button id="rSel"><b>Solo un tratto</b><small>da … a …</small></button></div>
        <div class="rng" id="rBox" hidden>da <input id="from" placeholder="01:00:00:00"><button class="mini" id="ph_from" title="Prendi la playhead di Resolve">◎</button>
          a <input id="to" placeholder="01:05:00:00"><button class="mini" id="ph_to" title="Prendi la playhead di Resolve">◎</button></div></div>
      <div><label>&nbsp;</label><div class="hint" style="margin:0">Tocca solo il video: l'audio della timeline resta esattamente com'è.</div></div>
    </div>
  </section>
  <section class="card wide">
    <h2><span class="n">2</span> Ritmo e quando zoomare</h2>
    <label>Ritmo degli zoom</label>
    <div class="seg" id="pace"></div>
    <div class="cols3">
      <label class="opt"><button class="switch on" disabled title="Sempre attivo"><i></i></button><span class="t"><b>Tagli</b><small>all'inizio delle clip (sempre)</small></span></label>
      <label class="opt"><button class="switch" id="t_speech"><i></i></button><span class="t"><b>Inizio di frase</b><small>quando riprende a parlare</small></span></label>
      <label class="opt"><button class="switch" id="t_emph"><i></i></button><span class="t"><b>Enfasi</b><small>quando la voce si alza</small></span></label>
    </div>
  </section>
  <section class="card wide">
    <h2><span class="n">3</span> Quanto, dove e come</h2>
    <div class="anchor">
      <div><label>Punto d'ancoraggio — clicca dove tenere il centro dello zoom (es. il volto)</label>
        <div class="frame" id="frame"><span id="noFrame">L'immagine compare dopo «Analizza».</span><img id="img" alt="" hidden><div class="zb" id="zb"></div><div class="x" id="x"></div></div></div>
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label>Zoom minimo</label><div class="slider"><input type="range" id="zmin" min="102" max="200" step="1"><output id="zminV"></output></div></div>
        <div><label>Zoom massimo</label><div class="slider"><input type="range" id="zmax" min="102" max="200" step="1"><output id="zmaxV"></output></div></div>
        <div><label>Stili (se ne scegli più d'uno, si alternano)</label>
          <div class="styles"><button class="tchip" data-s="smooth"><b>Morbido</b><small>entra ed esce dolce</small></button>
            <button class="tchip" data-s="jump"><b>Scatto</b><small>di colpo, senza animazione</small></button>
            <button class="tchip" data-s="snap"><b>Rapido</b><small>entra veloce, poi fermo</small></button></div></div>
        <label class="opt"><button class="switch" id="drift"><i></i></button><span class="t"><b>Movimento di camera</b><small>leggera deriva a mano durante lo zoom</small></span></label>
      </div>
    </div>
    <button class="btn primary" id="bAnalyze">Analizza e prepara gli zoom</button>
    <div class="hint" id="ffw" hidden>⚠ ffmpeg non trovato: senza, gli zoom vanno solo sui tagli.</div>
  </section>
  <section class="card wide">
    <div class="row2"><h2 style="margin:0">Anteprima degli zoom</h2><span style="flex:1"></span>
      <span class="hint" style="margin:0">clic = porta lì la playhead di Resolve</span>
      <button class="btn ghost" id="bShuffle" title="Altri zoom con le stesse regole">🎲 Rimescola</button></div>
    <div class="prev" id="prevBox"><canvas id="cv"></canvas><div class="tip2" id="tip"></div></div>
    <div class="stats" id="stats"></div>
  </section>
  <section class="card wide">
    <div class="row2"><button class="btn cta" id="bApply" style="flex:1">🔍 Applica gli zoom</button>
      <button class="btn" id="bUndo" title="Toglie tutti gli zoom ZoomTonic dalla traccia">↺ Togli gli zoom</button></div>
    <div class="hint" style="margin:0" id="zInfo"></div>
  </section>
  <section class="card wide">
    <div class="status"><span class="dot"></span><span id="status">Pronto.</span><span class="spacer"></span>
      <button class="btn ghost" id="bQuit">Chiudi ZoomTonic</button></div>
    <pre class="log" id="log"></pre>
  </section>
</main>
<div id="off" class="overlay" hidden><div class="card"><h2>ZoomTonic è chiuso</h2><p>Riaprilo da PlutoniCut o da Workspace ▸ Scripts ▸ Utility.</p></div></div>
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

const BUSY = {refresh: "Leggo la timeline…", analyze: "Leggo le clip e ascolto la voce…", apply: "Metto gli zoom nelle clip…",
  undo: "Tolgo gli zoom…", quit: "Chiudo…"};
const BTN = {refresh: "bRefresh", analyze: "bAnalyze", apply: "bApply", undo: "bUndo", quit: "bQuit"};
const SCOL = {smooth: "#8b5cf6", jump: "#f59e0b", snap: "#22c55e"}, SNAME = {smooth: "Morbido", jump: "Scatto", snap: "Rapido"};
let P = null, planT = null, planAt = -1, ax = .5, ay = .4, frameV = -1;
const on = id => $(id).classList.contains("on");
function collect() {
  clearTimeout(planT); planT = setTimeout(loadPlan, 350);
  return Object.assign(sectionGet(), {track: chosen("track")[0] || 1, voice: chosen("voice")[0] || 0, pace: val2("pace"),
    t_speech: on("t_speech"), t_emph: on("t_emph"), drift: on("drift"), zmin: +$("zmin").value, zmax: +$("zmax").value,
    styles: [...document.querySelectorAll(".styles .tchip.on")].map(b => b.dataset.s), ax: ax, ay: ay});
}
function marker() {                            // mirino e riquadro dello zoom massimo
  $("x").style.left = ax * 100 + "%"; $("x").style.top = ay * 100 + "%";
  const z = +$("zmax").value / 100, w = 100 / z, h = 100 / z;
  const l = Math.min(100 - w, Math.max(0, ax * 100 - ax * w)), t = Math.min(100 - h, Math.max(0, ay * 100 - ay * h));
  Object.assign($("zb").style, {left: l + "%", top: t + "%", width: w + "%", height: h + "%"});
}
$("frame").addEventListener("click", e => {
  const r = $("frame").getBoundingClientRect();
  ax = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); ay = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  marker(); push();
});
async function loadPlan() {
  try { P = await (await fetch("/api/plan", {cache: "no-store"})).json(); planAt = ST ? ST.anver : -1; draw();
    if (P.frame && frameV !== ST.anver) { frameV = ST.anver; $("img").src = "/api/frame?v=" + frameV; $("img").hidden = false; $("noFrame").hidden = true; }
  } catch (e) {}
}
const cv = $("cv");
function draw() {
  const W = cv.clientWidth, H = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const cs = getComputedStyle(document.body);
  if (!P || !P.clips.length) {
    g.fillStyle = cs.getPropertyValue("--muted"); g.font = "13px sans-serif"; g.textAlign = "center";
    g.fillText("Premi «Analizza e prepara gli zoom».", W / 2, H / 2); $("stats").innerHTML = ""; return;
  }
  const X = f => (f - P.a) / (P.b - P.a) * W;
  P.clips.forEach(([s, e, o], i) => {             // le clip della traccia
    g.fillStyle = o ? "rgba(239,68,68,.25)" : (i % 2 ? "rgba(128,128,128,.22)" : "rgba(128,128,128,.12)");
    g.fillRect(X(s), H - 26, Math.max(1, X(e) - X(s) - 1), 20);
  });
  if (P.voice.length) { g.fillStyle = cs.getPropertyValue("--muted");
    for (let x = 0; x < W; x++) { const v = Math.max(0, (P.voice[Math.floor(x / W * P.voice.length)] + 60) / 60); g.fillRect(x, 20 - v * 16, 1, v * 16); } }
  const top = 26, bot = H - 32, zr = Math.max(1.01, +$("zmax").value / 100);
  for (const [s, e, z, st] of P.zooms) {          // gli zoom: altezza = quanto zooma
    const hh = (z - 1) / (zr - 1) * (bot - top) * .9 + 6;
    g.fillStyle = SCOL[st]; g.fillRect(X(s), bot - hh, Math.max(2, X(e) - X(s)), hh);
  }
  const by = {};
  for (const z of P.zooms) by[z[3]] = (by[z[3]] || 0) + 1;
  $("stats").innerHTML = "<span class='chip'><b>" + P.zooms.length + "</b> zoom</span>" +
    Object.entries(by).map(([k, v]) => "<span class='chip'><i style='display:inline-block;width:10px;height:10px;border-radius:3px;background:" +
      SCOL[k] + "'></i> " + SNAME[k] + " <b>" + v + "</b></span>").join("") +
    (P.clips.some(c => c[2]) ? "<span class='chip'>in rosso: clip con una loro composizione Fusion (saltate)</span>" : "");
}
const fAt = px => P.a + px / cv.clientWidth * (P.b - P.a);
const tcOf = f => { const fi = Math.round(P.fps); f = Math.round(f);
  return [Math.floor(f / (fi * 3600)), Math.floor(f / (fi * 60)) % 60, Math.floor(f / fi) % 60, f % fi].map(x => String(x).padStart(2, "0")).join(":"); };
cv.addEventListener("pointermove", e => { if (!P || !P.clips.length) return; const f = fAt(e.offsetX), z = P.zooms.find(q => q[0] <= f && f < q[1]), t = $("tip");
  t.style.display = "block"; t.style.left = Math.min(e.offsetX + 12, cv.clientWidth - 220) + "px"; t.style.top = "24px";
  t.textContent = tcOf(f) + (z ? " · zoom " + Math.round(z[2] * 100) + "% " + SNAME[z[3]] + " (" + z[4] + ")" : ""); });
cv.addEventListener("pointerleave", () => { $("tip").style.display = "none"; });
cv.addEventListener("click", e => { if (P && P.clips.length) cmd("goto", tcOf(fAt(e.offsetX)), true); });
window.addEventListener("resize", draw);
$("bShuffle").onclick = () => { cmd("shuffle", null, true); setTimeout(loadPlan, 400); };
function init() {
  const s = ST.set;
  sectionUI(); sectionSync(s);
  seg2("pace", [["molto_calmo", "Molto calmo"], ["calmo", "Calmo"], ["moderato", "Moderato"], ["energico", "Energico"], ["iperattivo", "Iperattivo"]], s.pace);
  for (const k of ["t_speech", "t_emph", "drift"]) { $(k).classList.toggle("on", !!s[k]); $(k).onclick = e => { e.preventDefault(); $(k).classList.toggle("on"); push(); }; }
  for (const k of ["zmin", "zmax"]) {
    $(k).value = s[k]; $(k + "V").textContent = s[k] + "%";
    $(k).addEventListener("input", () => {
      if (+$("zmin").value > +$("zmax").value) $(k === "zmin" ? "zmax" : "zmin").value = $(k).value;
      for (const q of ["zmin", "zmax"]) $(q + "V").textContent = $(q).value + "%";
      marker(); draw(); });
    $(k).addEventListener("change", () => push());
  }
  for (const b of document.querySelectorAll(".styles .tchip")) {
    b.classList.toggle("on", (s.styles || []).includes(b.dataset.s));
    b.onclick = () => { b.classList.toggle("on"); if (!document.querySelector(".styles .tchip.on")) b.classList.add("on"); push(); };
  }
  ax = s.ax; ay = s.ay; marker();
  seenPh = ST.playhead.seq;
}
let lastTr = "";
function render() {
  const s = ST.src;
  $("tlName").textContent = s.ok ? s.name : "nessuna timeline aperta";
  $("tlInfo").textContent = s.ok ? s.tracks.video.length + " tracce video · " + s.tracks.audio.length + " tracce audio" : "apri una timeline in Resolve";
  const sig = JSON.stringify(s.tracks) + ST.set.track + "|" + ST.set.voice;
  if (sig !== lastTr) { lastTr = sig; chips("track", s.tracks.video, [ST.set.track], true); chips("voice", s.tracks.audio, [ST.set.voice], true); }
  $("ffw").hidden = ST.ffmpeg;
  $("bAnalyze").textContent = ST.fresh ? "↻ Analizza di nuovo" : "Analizza e prepara gli zoom";
  $("zInfo").textContent = s.zoomed ? "Su questa traccia " + s.zoomed + " clip hanno già gli zoom ZoomTonic: applicando di nuovo vengono rifatti."
    : "Gli zoom vanno in una composizione Fusion «ZoomTonic» su ogni clip: solo il video cambia, l'audio resta com'è.";
  if (ST.anver !== planAt) loadPlan();
  if (!ST.busy) $("bUndo").disabled = !s.zoomed;
  playheadIn();
}
start();
</script>
</body>
</html>
"""


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

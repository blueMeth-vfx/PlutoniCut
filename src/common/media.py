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

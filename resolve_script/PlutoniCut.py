"""
PlutoniCut - GUI per DaVinci Resolve (Workspace > Scripts > Utility > PlutoniCut)

Apre una tabella dove inserisci gli IN/OUT dei tagli (timecode SRT tipo
01:00:37,958) e li applica alla timeline APERTA in quel momento, prendendo come
sorgente la clip selezionata nel Media Pool. Puoi riempire la tabella caricando
un cut.json.

Self-contained: nessuna dipendenza esterna, gira con il Python interno di Resolve.
"""

import datetime
import json
import math
import os
import re

_LOGO = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design",
                     "DaVinci Resolve", "Support", "Fusion", "Scripts", "Utility",
                     "plutonic_logo.png")

# --------------------------------------------------------------------------
# Connessione a Resolve / Fusion (robusta sia da menu Scripts sia da Console)
# --------------------------------------------------------------------------
try:
    resolve  # iniettata quando si lancia da Workspace > Scripts
except NameError:
    import DaVinciResolveScript as _dvr
    resolve = _dvr.scriptapp("Resolve")

fusion = resolve.Fusion()
ui = fusion.UIManager
try:
    disp = bmd.UIDispatcher(ui)          # bmd globale dentro Resolve
except NameError:
    import fusionscript as bmd
    disp = bmd.UIDispatcher(ui)


# --------------------------------------------------------------------------
# Logica pura (timecode + frame). Identica a quella della CLI MakeACut.
# --------------------------------------------------------------------------
_RATIONAL_FPS = {23.976: 24000 / 1001, 29.97: 30000 / 1001,
                 47.952: 48000 / 1001, 59.94: 60000 / 1001,
                 119.88: 120000 / 1001}
_SRT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*$")


def norm_fps(fps):
    f = float(str(fps).lower().replace("fps", "").strip())
    for approx, exact in _RATIONAL_FPS.items():
        if abs(f - approx) < 0.01:
            return exact
    return f


def parse_srt_tc(tc):
    m = _SRT_RE.match(str(tc))
    if not m:
        raise ValueError("Timecode non valido: %r (atteso HH:MM:SS,mmm)" % tc)
    h, mnt, s, ms = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def to_frames(seconds, fps):
    return int(round(seconds * norm_fps(fps)))


# --------------------------------------------------------------------------
# Stato applicativo: ROWS e' la fonte di verita' (in, out, nome, nota).
# --------------------------------------------------------------------------
ROWS = []          # list of dict(in=, out=, name=, note=)
ITEMS = []         # oggetti riga del Tree, in ordine parallelo a ROWS

COLS = ["In", "Out", "Nome", "Nota"]

# --- Tema scuro (Qt stylesheet) ------------------------------------------
# --------------------------------------------------------------------------
# Temi Plutonic: Scuro (Plutonic Studio, viola) / Chiaro (Plutonic Labs, tramonto)
# La scelta e' ricordata (condivisa con BeaTonic) in %APPDATA%\Plutonic\theme.txt
# --------------------------------------------------------------------------
_THEME_FILE = os.path.join(os.environ.get("APPDATA", ""), "Plutonic", "theme.txt")


def load_theme():
    try:
        with open(_THEME_FILE, "r") as f:
            t = f.read().strip()
        return t if t in ("studio", "labs") else "studio"
    except Exception:
        return "studio"


def save_theme(name):
    try:
        os.makedirs(os.path.dirname(_THEME_FILE), exist_ok=True)
        with open(_THEME_FILE, "w") as f:
            f.write(name)
    except Exception:
        pass


_PAL = {
    "studio": {
        "font": "'Segoe UI','Inter','Helvetica Neue',sans-serif",
        "hfont": "'Segoe UI','Inter',sans-serif",
        "bg": "#0f1115", "panel": "#111827", "tree": "#0b0d11", "alt": "#111827",
        "fg": "#e5e7eb", "muted": "#9ca3af", "border": "#1f2937", "border2": "#374151",
        "bw": "1px", "radius": "8px", "accent": "#7c3aed", "focus": "#8b5cf6",
        "header": "#a78bfa", "sec": "#9f86e0", "selfg": "#ffffff",
        "primaryBg": "#7c3aed", "primaryHover": "#8b5cf6",
        "primaryFg": "#ffffff", "primaryBorder": "border:0;",
    },
    "labs": {
        "font": "'Public Sans','Segoe UI',sans-serif",
        "hfont": "'Rubik Mono One','Impact',sans-serif",
        "bg": "#F3F0F7", "panel": "#FBF4FF", "tree": "#FBF4FF", "alt": "#F3F0F7",
        "fg": "#0E0418", "muted": "#4B4458", "border": "#0E0418", "border2": "#0E0418",
        "bw": "2px", "radius": "6px", "accent": "#EE3A5B", "focus": "#EE3A5B",
        "header": "#B0126E", "sec": "#B0126E", "selfg": "#ffffff",
        "primaryBg": "qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #F7A541, stop:1 #EE3A5B)",
        "primaryHover": "qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #F4F07C, stop:1 #F7A541)",
        "primaryFg": "#0E0418", "primaryBorder": "border:2px solid #0E0418;",
    },
}

_TMPL = """
* { font-family: %(font)s; font-size: 13px; color: %(fg)s; outline: none; }
QWidget { background-color: %(bg)s; }
QLabel#Header { font-family: %(hfont)s; font-size: 20px; font-weight: 800; color: %(header)s; }
QPushButton#Logo { background: transparent; border: 0; margin: 0px; }
QPushButton#ThemeBtn { background: transparent; border: %(bw)s solid %(border2)s;
    border-radius: %(radius)s; padding: 4px 9px; color: %(muted)s; font-weight: 700; }
QPushButton#ThemeBtn:hover { border: %(bw)s solid %(accent)s; color: %(fg)s; }
QLabel#Src { color: %(muted)s; padding: 4px 2px; }
QLineEdit, QComboBox { background: %(panel)s; border: %(bw)s solid %(border)s;
    border-radius: %(radius)s; padding: 6px 9px; margin: 0px 3px;
    selection-background-color: %(accent)s; selection-color: %(selfg)s; }
QLineEdit:focus, QComboBox:focus { border: %(bw)s solid %(focus)s; }
QComboBox::drop-down { border: 0; width: 18px; }
QPushButton { background: %(panel)s; border: %(bw)s solid %(border2)s;
    border-radius: %(radius)s; padding: 7px 13px; margin: 0px 3px; }
QPushButton:hover { background: %(border)s; }
QPushButton:pressed { background: %(tree)s; }
QPushButton#Apply { background: %(primaryBg)s; color: %(primaryFg)s; font-weight: 700;
    %(primaryBorder)s padding: 9px 13px; }
QPushButton#Apply:hover { background: %(primaryHover)s; }
QPushButton#Close { color: %(muted)s; }
QCheckBox { spacing: 6px; margin: 0px 3px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px;
    border: %(bw)s solid %(border2)s; background: %(panel)s; }
QCheckBox::indicator:checked { background: %(accent)s; border: %(bw)s solid %(accent)s; }
QTreeWidget, QTreeView { background: %(tree)s; border: %(bw)s solid %(border)s;
    border-radius: 10px; alternate-background-color: %(alt)s; }
QHeaderView::section { background: %(panel)s; color: %(muted)s; border: 0;
    border-bottom: %(bw)s solid %(border)s; padding: 7px; font-weight: 700; }
QTreeWidget::item { padding: 4px; }
QTreeWidget::item:selected { background: %(accent)s; color: %(selfg)s; }
QLabel#Status { color: %(muted)s; background: %(tree)s; border: %(bw)s solid %(border)s;
    border-radius: %(radius)s; padding: 7px 10px; }
"""

STYLE_FOR = {name: _TMPL % pal for name, pal in _PAL.items()}
THEME = load_theme()
STYLE = STYLE_FOR[THEME]
_SECIDS = []


def sec_style(theme):
    return ("color:%s; font-size:11px; font-weight:700; letter-spacing:1px;"
            " padding-top:8px;") % _PAL[theme]["sec"]


def _sec(text):
    sid = "Sec%d" % len(_SECIDS)
    _SECIDS.append(sid)
    return ui.Label({"ID": sid, "Text": text, "Weight": 0,
                     "StyleSheet": sec_style(THEME)})


win = disp.AddWindow({
    "ID": "PlutoniCutWin",
    "WindowTitle": "PlutoniCut",
    "Geometry": [100, 80, 1000, 600],
    "StyleSheet": STYLE,
}, [
    ui.VGroup({"Spacing": 7, "Margin": 14}, [
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.Button({"ID": "Logo", "Flat": True, "Weight": 0,
                       "MinimumSize": [62, 48], "MaximumSize": [62, 48]}),
            ui.Label({"ID": "Header", "Text": "PlutoniCut", "Weight": 0}),
            ui.Label({"Text": "tieni solo gli in/out, taglia il resto",
                      "Weight": 1, "Alignment": {"AlignBottom": True}}),
            ui.Button({"ID": "ThemeBtn", "Text": "☀  Tema Labs", "Weight": 0}),
        ]),
        ui.Label({"ID": "Src", "Text": "Timeline: (apri una timeline, poi Aggiorna)",
                  "Weight": 0}),

        _sec("TAGLI  —  in / out = timecode della timeline da tenere"),
        ui.Tree({"ID": "Tree", "Weight": 1, "AlternatingRowColors": True,
                 "RootIsDecorated": False, "ItemsExpandable": False}),
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.LineEdit({"ID": "In", "PlaceholderText": "In  01:00:34,830", "Weight": 3}),
            ui.LineEdit({"ID": "Out", "PlaceholderText": "Out 01:03:15,870", "Weight": 3}),
            ui.LineEdit({"ID": "Name", "PlaceholderText": "Nome", "Weight": 3}),
            ui.LineEdit({"ID": "Note", "PlaceholderText": "Nota", "Weight": 4}),
            ui.Button({"ID": "Add", "Text": "+ Riga", "Weight": 1}),
            ui.Button({"ID": "Del", "Text": "– Riga", "Weight": 1}),
        ]),

        _sec("AUTOCOMPILA DA FILE"),
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.LineEdit({"ID": "Path", "PlaceholderText": "percorso di cut.json", "Weight": 6}),
            ui.Button({"ID": "Browse", "Text": "Sfoglia", "Weight": 1}),
            ui.Button({"ID": "Load", "Text": "Carica", "Weight": 1}),
        ]),

        _sec("MONTAGGIO  —  l'originale resta intatta (backup automatico)"),
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.LineEdit({"ID": "TlName", "PlaceholderText": "Nome timeline montata", "Weight": 5}),
            ui.LineEdit({"ID": "Offset", "PlaceholderText": "Offset s", "Weight": 2}),
            ui.ComboBox({"ID": "Color", "Weight": 2}),
            ui.CheckBox({"ID": "Backup", "Text": "Copia _BACKUP", "Checked": False, "Weight": 2}),
        ]),

        ui.VGap(4),
        ui.HGroup({"Weight": 0, "Spacing": 0}, [
            ui.Button({"ID": "Apply", "Text": "▶  Monta la timeline", "Weight": 3}),
            ui.Button({"ID": "Refresh", "Text": "Aggiorna", "Weight": 1}),
            ui.Button({"ID": "Close", "Text": "Chiudi", "Weight": 1}),
        ]),
        ui.Label({"ID": "Status", "Text": "Pronto.", "Weight": 0,
                  "WordWrap": True}),
    ]),
])

itm = win.GetItems()

# Logo Plutonic nell'header (se il file e la build lo consentono)
try:
    if os.path.exists(_LOGO):
        itm["Logo"].Icon = ui.Icon({"File": _LOGO})
        itm["Logo"].IconSize = [58, 44]
except Exception:
    pass

# Header + colonne
try:
    itm["Tree"].ColumnCount = len(COLS)
    itm["Tree"].SetHeaderLabels(COLS)
except Exception:
    pass

# Colori marker disponibili in Resolve
for c in ["Yellow", "Blue", "Cyan", "Green", "Red", "Pink",
          "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint",
          "Lemon", "Sand", "Cocoa", "Cream"]:
    itm["Color"].AddItem(c)


def set_status(msg):
    itm["Status"].Text = msg


def render():
    """Ridisegna il Tree da ROWS. ROWS resta la fonte di verita'."""
    global ITEMS
    tree = itm["Tree"]
    try:
        tree.Clear()
    except Exception:
        # fallback se Clear non c'e': rimuovi a mano
        try:
            while tree.TopLevelItemCount > 0:
                tree.TakeTopLevelItem(0)
        except Exception:
            pass
    ITEMS = []
    for r in ROWS:
        it = tree.NewItem()
        it.Text[0] = r["in"]
        it.Text[1] = r["out"]
        it.Text[2] = r.get("name", "")
        it.Text[3] = r.get("note", "") or ""
        try:
            it.Flags = {"Selectable": True, "Enabled": True, "Editable": True}
        except Exception:
            pass
        tree.AddTopLevelItem(it)
        ITEMS.append(it)


def sync_from_tree():
    """Recupera eventuali modifiche inline (se la build le supporta)."""
    for i, it in enumerate(ITEMS):
        if i < len(ROWS):
            try:
                ROWS[i]["in"] = it.Text[0]
                ROWS[i]["out"] = it.Text[1]
                ROWS[i]["name"] = it.Text[2]
                ROWS[i]["note"] = it.Text[3]
            except Exception:
                pass


# ----------------------------------------------------------- timeline aperta

def current_timeline():
    """La timeline aperta: e' lei la sorgente da montare (non tocco i media)."""
    proj = resolve.GetProjectManager().GetCurrentProject()
    if not proj:
        return None, "Nessun progetto aperto."
    tl = proj.GetCurrentTimeline()
    if not tl:
        return None, "Apri la timeline da montare, poi 'Aggiorna'."
    try:
        fps = tl.GetSetting("timelineFrameRate")
    except Exception:
        fps = "?"
    n = 0
    try:
        for ti in range(1, (tl.GetTrackCount("video") or 0) + 1):
            n += len(tl.GetItemListInTrack("video", ti) or [])
    except Exception:
        pass
    return tl, "Timeline: %s   -   %d clip video   -   %s fps" % (tl.GetName(), n, fps)


def refresh_source(ev=None):
    tl, msg = current_timeline()
    itm["Src"].Text = msg
    return tl


# ---------------------------------------------------------------- eventi UI

def on_add(ev):
    inv = itm["In"].Text.strip()
    outv = itm["Out"].Text.strip()
    if not inv or not outv:
        set_status("Inserisci sia In sia Out.")
        return
    try:
        if parse_srt_tc(outv) <= parse_srt_tc(inv):
            set_status("Out deve essere maggiore di In.")
            return
    except ValueError as e:
        set_status(str(e))
        return
    ROWS.append({"in": inv, "out": outv,
                 "name": itm["Name"].Text.strip() or "cap %d" % (len(ROWS) + 1),
                 "note": itm["Note"].Text.strip()})
    render()
    for f in ("In", "Out", "Name", "Note"):
        itm[f].Text = ""
    set_status("%d righe in tabella." % len(ROWS))


def on_del(ev):
    tree = itm["Tree"]
    cur = None
    try:
        cur = tree.CurrentItem()
    except Exception:
        cur = None
    idx = None
    if cur is not None:
        for i, it in enumerate(ITEMS):
            if it == cur:
                idx = i
                break
    if idx is None and ROWS:
        idx = len(ROWS) - 1  # fallback: ultima riga
    if idx is None:
        set_status("Niente da rimuovere.")
        return
    ROWS.pop(idx)
    render()
    set_status("Rimossa riga %d. %d righe." % (idx + 1, len(ROWS)))


def on_browse(ev):
    # Prova un file dialog Fusion; se non c'e', chiedi di incollare il percorso.
    path = None
    for attempt in (
        lambda: fusion.RequestFile(""),
        lambda: fusion.RequestFile(),
    ):
        try:
            path = attempt()
            if path:
                break
        except Exception:
            continue
    if path:
        itm["Path"].Text = path
        set_status("Percorso selezionato. Premi 'Carica cut.json'.")
    else:
        set_status("Dialog non disponibile: incolla il percorso nel campo e premi Carica.")


def on_load(ev):
    path = itm["Path"].Text.strip().strip('"')
    if not path:
        set_status("Indica il percorso di cut.json.")
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        set_status("Errore lettura json: %s" % e)
        return
    segs = data.get("segments", [])
    if not segs:
        set_status("cut.json senza 'segments'.")
        return
    bad = 0
    del ROWS[:]
    for s in segs:
        try:
            inv, outv = s["in"], s["out"]
            parse_srt_tc(inv)
            parse_srt_tc(outv)
        except Exception:
            bad += 1
            continue
        ROWS.append({"in": inv, "out": outv,
                     "name": s.get("name", "cap %d" % (len(ROWS) + 1)),
                     "note": s.get("note", "")})
    # precompila nome nuova timeline se presente
    if data.get("timeline_name"):
        itm["TlName"].Text = data["timeline_name"]
    render()
    msg = "Caricati %d segmenti da cut.json." % len(ROWS)
    if bad:
        msg += " %d scartati (timecode non valido)." % bad
    set_status(msg)


def on_apply(ev):
    sync_from_tree()
    if not ROWS:
        set_status("Tabella vuota.")
        return
    src_tl = refresh_source()
    if not src_tl:
        return
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()

    try:
        fps = norm_fps(src_tl.GetSetting("timelineFrameRate"))
    except Exception:
        set_status("Frame rate della timeline non leggibile.")
        return

    # Ricostruisco OGNI traccia (video E audio) in modo indipendente, cosi' il
    # montato porta l'audio che c'e' in timeline (es. recorder sincronizzato su una
    # traccia separata), NON lo scratch embedded delle clip video.
    nv = src_tl.GetTrackCount("video") or 0
    na = src_tl.GetTrackCount("audio") or 0
    vitems, aitems = [], []
    try:
        for ti in range(1, nv + 1):
            for it in (src_tl.GetItemListInTrack("video", ti) or []):
                vitems.append((it, ti))
        for ti in range(1, na + 1):
            for it in (src_tl.GetItemListInTrack("audio", ti) or []):
                aitems.append((it, ti))
    except Exception:
        vitems, aitems = [], []
    if not vitems:
        set_status("La timeline aperta non ha clip video.")
        return
    vitems.sort(key=lambda x: x[0].GetStart())
    aitems.sort(key=lambda x: x[0].GetStart())
    # Se ci sono tracce audio, le ricostruisco separate (video->solo video,
    # audio->solo audio). Se non c'e' audio, tengo il video con il suo audio.
    rebuild_audio = len(aitems) > 0
    tl_start, tl_end = src_tl.GetStartFrame(), src_tl.GetEndFrame()

    # offset opzionale in secondi (0 = nessuno). Gli in/out sono TC di timeline.
    manual = itm["Offset"].Text.strip()
    off_f = 0
    if manual and manual not in ("0", "auto"):
        try:
            off_f = int(round(float(manual.replace(",", ".")) * fps))
        except ValueError:
            set_status("Offset non numerico (secondi).")
            return

    # righe -> range assoluti in frame di timeline
    try:
        segs = [(int(round(parse_srt_tc(r["in"]) * fps)) + off_f,
                 int(round(parse_srt_tc(r["out"]) * fps)) + off_f, r)
                for r in ROWS]
    except ValueError as e:
        set_status(str(e))
        return

    # Copia extra opzionale dell'originale (l'originale resta comunque intatta).
    backup_note = ""
    if itm["Backup"].Checked:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            bk = src_tl.DuplicateTimeline("%s_BACKUP_%s" % (src_tl.GetName(), stamp))
        except Exception:
            bk = None
        if bk is not None:
            backup_note = " Copia: '%s'." % bk.GetName()

    # Timeline MONTATA: nuova, ricostruita coi soli range tenuti. L'API non sa
    # fare razor in-place, quindi l'originale non viene modificata (= backup).
    name = itm["TlName"].Text.strip() or (src_tl.GetName() + "_CUT")
    new_tl = mp.CreateEmptyTimeline(name)
    k = 2
    while new_tl is None and k < 100:
        new_tl = mp.CreateEmptyTimeline("%s (%d)" % (name, k))
        k += 1
    if new_tl is None:
        set_status("Impossibile creare la timeline montata.")
        return
    proj.SetCurrentTimeline(new_tl)

    # Assicura abbastanza tracce nel montato per rispettare gli indici originali.
    try:
        while (new_tl.GetTrackCount("video") or 1) < nv:
            new_tl.AddTrack("video")
        while (new_tl.GetTrackCount("audio") or 1) < na:
            new_tl.AddTrack("audio")
    except Exception:
        pass
    new_start = new_tl.GetStartFrame()

    color = itm["Color"].CurrentText or "Yellow"
    ok = 0
    fails = []
    oob = 0
    cursor = 0  # frame COMPATTATO nel montato: avanza solo del contenuto reale
    all_items = vitems + aitems
    for a, b, r in segs:
        if b <= a:
            fails.append((r["name"], "out <= in"))
            continue
        if a < tl_start or b > tl_end:
            oob += 1
        seg_start = cursor
        # Confini di TUTTE le clip (video+audio) dentro [a,b) -> sotto-intervalli in cui
        # l'insieme dei clip che coprono e' costante. Cosi' tengo le tracce in PARALLELO.
        pts = {a, b}
        for it, ti in all_items:
            s, e = it.GetStart(), it.GetEnd()
            if a < s < b:
                pts.add(s)
            if a < e < b:
                pts.add(e)
        subs = sorted(pts)
        placed = False
        for x, y in zip(subs, subs[1:]):
            if y <= x:
                continue
            rec = new_start + cursor          # posizione compattata del sotto-intervallo
            # VIDEO che coprono [x,y], per traccia
            vpieces = []
            for it, ti in vitems:
                s, e = it.GetStart(), it.GetEnd()
                if s <= x and e >= y:
                    mpi = it.GetMediaPoolItem()
                    s0 = it.GetSourceStartFrame()
                    if mpi is not None and s0 is not None:
                        vpieces.append((ti, {"mediaPoolItem": mpi,
                                             "startFrame": s0 + (x - s),
                                             "endFrame": s0 + (y - s), "trackIndex": ti}))
            if not vpieces:
                continue                      # nessun video qui: compatta via (niente avanzamento)
            vpieces.sort(key=lambda z: z[0])
            # AUDIO che coprono [x,y]
            apieces = []
            if rebuild_audio:
                for it, ti in aitems:
                    s, e = it.GetStart(), it.GetEnd()
                    if s <= x and e >= y:
                        mpa = it.GetMediaPoolItem()
                        s0 = it.GetSourceStartFrame()
                        if mpa is not None and s0 is not None:
                            apieces.append({"mediaPoolItem": mpa,
                                            "startFrame": s0 + (x - s),
                                            "endFrame": s0 + (y - s),
                                            "trackIndex": ti, "mediaType": 2})
            # ANCORA: video della traccia piu' bassa + audio, append SEQUENZIALE (gapless)
            anchor = vpieces[0][1]
            if rebuild_audio:
                anchor["mediaType"] = 1
            try:
                if mp.AppendToTimeline([anchor] + apieces):
                    placed = True
            except Exception as ex:
                fails.append((r["name"], str(ex)))
                continue
            # ALTRE tracce video -> recordFrame alla STESSA posizione (si impilano allineate)
            for ti, info in vpieces[1:]:
                info["mediaType"] = 1
                info["recordFrame"] = rec
                try:
                    mp.AppendToTimeline([info])
                except Exception:
                    pass
            cursor += (y - x)
        if not placed:
            fails.append((r["name"], "nessuna clip nel range"))
            continue
        ok += 1
        try:
            new_tl.AddMarker(int(seg_start), color, r["name"], r.get("note") or "", 1, "")
        except Exception:
            pass

    msg = "Montata '%s': %d/%d segmenti, marker %s.%s" % (
        new_tl.GetName(), ok, len(segs), color, backup_note)
    if oob:
        msg += " %d range fuori dalla timeline." % oob
    if fails:
        msg += " Falliti: " + "; ".join("%s (%s)" % f for f in fails[:3])
    set_status(msg)


def on_close(ev):
    disp.ExitLoop()


win.On.Add.Clicked = on_add
win.On.Del.Clicked = on_del
win.On.Browse.Clicked = on_browse
win.On.Load.Clicked = on_load
win.On.Apply.Clicked = on_apply
win.On.Refresh.Clicked = refresh_source
win.On.Close.Clicked = on_close
win.On.PlutoniCutWin.Close = on_close


def apply_theme(name):
    global THEME
    THEME = name
    done = False
    try:
        win.SetStyleSheet(STYLE_FOR[name]); done = True
    except Exception:
        pass
    if not done:
        try:
            itm["PlutoniCutWin"].SetStyleSheet(STYLE_FOR[name])
        except Exception:
            pass
    for sid in _SECIDS:
        try:
            itm[sid].SetStyleSheet(sec_style(name))
        except Exception:
            pass
    try:
        itm["ThemeBtn"].Text = ("☀  Tema Labs" if name == "studio"
                                else "☾  Tema Studio")
    except Exception:
        pass
    save_theme(name)


def on_theme(ev=None):
    apply_theme("labs" if THEME == "studio" else "studio")


win.On.ThemeBtn.Clicked = on_theme
apply_theme(THEME)

refresh_source()
render()
win.Show()
disp.RunLoop()
win.Hide()

"""
MakeACut - GUI per DaVinci Resolve (Workspace > Scripts > Utility > MakeACut)

Apre una tabella dove inserisci gli IN/OUT dei tagli (timecode SRT tipo
01:00:37,958) e li applica alla timeline APERTA in quel momento, prendendo come
sorgente la clip selezionata nel Media Pool. Puoi riempire la tabella caricando
un cut.json.

Self-contained: nessuna dipendenza esterna, gira con il Python interno di Resolve.
"""

import datetime
import json
import math
import re

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
ACCENT = "#e8b33d"      # ambra, come i marker
STYLE = """
* { font-family: 'Segoe UI','Helvetica Neue',sans-serif; font-size: 13px;
    color: #e6e8ea; outline: none; }
QWidget { background-color: #1b1e23; }
QLabel#Header { font-size: 20px; font-weight: 800; color: %(a)s; }
QLabel#Src { color: #b8c0cc; padding: 4px 2px; }
QLineEdit, QComboBox { background: #262a31; border: 1px solid #3a3f47;
    border-radius: 5px; padding: 5px 7px; selection-background-color: %(a)s; }
QLineEdit:focus, QComboBox:focus { border: 1px solid %(a)s; }
QComboBox::drop-down { border: 0; width: 18px; }
QPushButton { background: #333842; border: 1px solid #444b55;
    border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #3d434e; }
QPushButton:pressed { background: #2b303a; }
QPushButton#Apply { background: %(a)s; color: #1b1e23; font-weight: 800;
    border: 0; padding: 9px 12px; }
QPushButton#Apply:hover { background: #f2c25a; }
QPushButton#Close { color: #c9ccd1; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid #4a515b; background: #262a31; }
QCheckBox::indicator:checked { background: %(a)s; border: 1px solid %(a)s; }
QTreeWidget, QTreeView { background: #15181c; border: 1px solid #333842;
    border-radius: 6px; alternate-background-color: #191d22; }
QHeaderView::section { background: #262a31; color: #aab2bd; border: 0;
    border-bottom: 1px solid #333842; padding: 6px; font-weight: 700; }
QTreeWidget::item { padding: 3px; }
QTreeWidget::item:selected { background: %(a)s; color: #1b1e23; }
QLabel#Status { color: #9aa2ad; background: #15181c; border: 1px solid #333842;
    border-radius: 5px; padding: 6px 9px; }
""" % {"a": ACCENT}


def _sec(text):
    return ui.Label({"Text": text, "Weight": 0, "StyleSheet": (
        "color:#8b929c; font-size:11px; font-weight:700; letter-spacing:1px;"
        " padding-top:8px;")})


win = disp.AddWindow({
    "ID": "MakeACutWin",
    "WindowTitle": "MakeACut",
    "Geometry": [100, 80, 1000, 600],
    "StyleSheet": STYLE,
}, [
    ui.VGroup({"Spacing": 7, "Margin": 14}, [
        ui.HGroup({"Weight": 0, "Spacing": 10}, [
            ui.Label({"ID": "Header", "Text": "✂  MakeACut", "Weight": 0}),
            ui.Label({"Text": "tieni solo gli in/out, taglia il resto",
                      "Weight": 1, "Alignment": {"AlignBottom": True}}),
        ]),
        ui.Label({"ID": "Src", "Text": "Timeline: (apri una timeline, poi Aggiorna)",
                  "Weight": 0}),

        _sec("TAGLI  —  in / out = timecode della timeline da tenere"),
        ui.Tree({"ID": "Tree", "Weight": 1, "AlternatingRowColors": True,
                 "RootIsDecorated": False, "ItemsExpandable": False}),
        ui.HGroup({"Weight": 0, "Spacing": 6}, [
            ui.LineEdit({"ID": "In", "PlaceholderText": "In  01:00:34,830",
                         "MinimumSize": [150, 0], "Weight": 0}),
            ui.LineEdit({"ID": "Out", "PlaceholderText": "Out 01:03:15,870",
                         "MinimumSize": [150, 0], "Weight": 0}),
            ui.LineEdit({"ID": "Name", "PlaceholderText": "Nome",
                         "MinimumSize": [150, 0], "Weight": 0}),
            ui.LineEdit({"ID": "Note", "PlaceholderText": "Nota",
                         "MinimumSize": [190, 0], "Weight": 0}),
            ui.Button({"ID": "Add", "Text": "+ Riga", "Weight": 0}),
            ui.Button({"ID": "Del", "Text": "– Riga", "Weight": 0}),
            ui.HGap(0, 1),
        ]),

        _sec("AUTOCOMPILA DA FILE"),
        ui.HGroup({"Weight": 0, "Spacing": 6}, [
            ui.LineEdit({"ID": "Path", "PlaceholderText": "percorso di cut.json",
                         "MinimumSize": [420, 0], "Weight": 0}),
            ui.Button({"ID": "Browse", "Text": "Sfoglia", "Weight": 0}),
            ui.Button({"ID": "Load", "Text": "Carica", "Weight": 0}),
            ui.HGap(0, 1),
        ]),

        _sec("MONTAGGIO  —  l'originale resta intatta (backup automatico)"),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"Text": "Nome", "Weight": 0}),
            ui.LineEdit({"ID": "TlName", "PlaceholderText": "nome timeline montata",
                         "MinimumSize": [240, 0], "Weight": 0}),
            ui.Label({"Text": "Offset s", "Weight": 0}),
            ui.LineEdit({"ID": "Offset", "PlaceholderText": "0",
                         "MinimumSize": [56, 0], "Weight": 0}),
            ui.Label({"Text": "Marker", "Weight": 0}),
            ui.ComboBox({"ID": "Color", "MinimumSize": [104, 0], "Weight": 0}),
            ui.CheckBox({"ID": "Backup", "Text": "Copia _BACKUP",
                         "Checked": False, "Weight": 0}),
            ui.HGap(0, 1),
        ]),

        ui.VGap(4),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Button({"ID": "Apply", "Text": "▶  Monta la timeline",
                       "MinimumSize": [320, 0], "Weight": 0}),
            ui.Button({"ID": "Refresh", "Text": "Aggiorna", "Weight": 0}),
            ui.Button({"ID": "Close", "Text": "Chiudi", "Weight": 0}),
            ui.HGap(0, 1),
        ]),
        ui.Label({"ID": "Status", "Text": "Pronto.", "Weight": 0,
                  "WordWrap": True}),
    ]),
])

itm = win.GetItems()

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

    # Le clip attualmente in timeline: da qui prendo i pezzi da tenere.
    items = []
    try:
        for ti in range(1, (src_tl.GetTrackCount("video") or 0) + 1):
            items += src_tl.GetItemListInTrack("video", ti) or []
    except Exception:
        items = []
    if not items:
        set_status("La timeline aperta non ha clip video.")
        return
    items.sort(key=lambda it: it.GetStart())
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

    color = itm["Color"].CurrentText or "Yellow"
    ok = 0
    fails = []
    oob = 0
    cursor = 0  # frame nella timeline montata -> posizione dei marker
    for a, b, r in segs:
        if b <= a:
            fails.append((r["name"], "out <= in"))
            continue
        if a < tl_start or b > tl_end:
            oob += 1
        # ritaglia i pezzi dagli item che intersecano [a, b)
        infos = []
        seg_len = 0
        for it in items:
            s, e = it.GetStart(), it.GetEnd()
            aa, bb = max(a, s), min(b, e)
            if bb <= aa:
                continue
            mpi = it.GetMediaPoolItem()
            if not mpi:
                continue
            src0 = it.GetSourceStartFrame()
            infos.append({"mediaPoolItem": mpi,
                          "startFrame": src0 + (aa - s),
                          "endFrame": src0 + (bb - s)})   # endFrame esclusivo
            seg_len += (bb - aa)
        if not infos:
            fails.append((r["name"], "nessuna clip nel range"))
            continue
        try:
            added = mp.AppendToTimeline(infos)
        except Exception as ex:
            fails.append((r["name"], str(ex)))
            continue
        if not added:
            fails.append((r["name"], "append vuoto"))
            continue
        ok += 1
        try:
            new_tl.AddMarker(int(cursor), color, r["name"], r.get("note") or "", 1, "")
        except Exception:
            pass
        cursor += seg_len

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
win.On.MakeACutWin.Close = on_close

refresh_source()
render()
win.Show()
disp.RunLoop()
win.Hide()

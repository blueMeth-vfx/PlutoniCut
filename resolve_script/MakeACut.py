"""
MakeACut - GUI per DaVinci Resolve (Workspace > Scripts > Utility > MakeACut)

Apre una tabella dove inserisci gli IN/OUT dei tagli (timecode SRT tipo
01:00:37,958) e li applica alla timeline APERTA in quel momento, prendendo come
sorgente la clip selezionata nel Media Pool. Puoi riempire la tabella caricando
un cut.json.

Self-contained: nessuna dipendenza esterna, gira con il Python interno di Resolve.
"""

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

win = disp.AddWindow({
    "ID": "MakeACutWin",
    "WindowTitle": "MakeACut - tagli sulla timeline aperta",
    "Geometry": [120, 120, 820, 560],
}, [
    ui.VGroup({"Spacing": 6, "Margin": 10}, [
        ui.Label({"Text": "MakeACut", "Weight": 0,
                  "Font": ui.Font({"PixelSize": 18, "Bold": True})}),
        ui.Label({"ID": "Src", "Text": "Sorgente: (nessuna clip selezionata)",
                  "Weight": 0}),
        ui.Tree({"ID": "Tree", "Weight": 1, "AlternatingRowColors": True}),
        ui.HGroup({"Weight": 0, "Spacing": 5}, [
            ui.LineEdit({"ID": "In", "PlaceholderText": "In  01:00:37,958"}),
            ui.LineEdit({"ID": "Out", "PlaceholderText": "Out 01:01:13,250"}),
            ui.LineEdit({"ID": "Name", "PlaceholderText": "Nome capitolo"}),
            ui.LineEdit({"ID": "Note", "PlaceholderText": "Nota (opzionale)"}),
            ui.Button({"ID": "Add", "Text": "Aggiungi", "Weight": 0}),
            ui.Button({"ID": "Del", "Text": "Rimuovi sel.", "Weight": 0}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 5}, [
            ui.LineEdit({"ID": "Path", "PlaceholderText": "percorso di cut.json"}),
            ui.Button({"ID": "Browse", "Text": "Sfoglia...", "Weight": 0}),
            ui.Button({"ID": "Load", "Text": "Carica cut.json", "Weight": 0}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 5}, [
            ui.CheckBox({"ID": "NewTl", "Text": "Crea nuova timeline", "Weight": 0}),
            ui.LineEdit({"ID": "TlName", "PlaceholderText": "nome nuova timeline"}),
            ui.Label({"Text": "Offset:", "Weight": 0}),
            ui.LineEdit({"ID": "Offset", "PlaceholderText": "auto", "Weight": 0}),
            ui.Label({"Text": "Marker:", "Weight": 0}),
            ui.ComboBox({"ID": "Color", "Weight": 0}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 5}, [
            ui.Button({"ID": "Apply", "Text": "Applica alla timeline", "Weight": 1}),
            ui.Button({"ID": "Refresh", "Text": "Aggiorna sorgente", "Weight": 0}),
            ui.Button({"ID": "Close", "Text": "Chiudi", "Weight": 0}),
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


# ---------------------------------------------------------------- sorgente

def current_source_clip():
    """Clip selezionata nel Media Pool (niente match sul nome)."""
    proj = resolve.GetProjectManager().GetCurrentProject()
    if not proj:
        return None, "Nessun progetto aperto."
    mp = proj.GetMediaPool()
    sel = None
    try:
        sel = mp.GetSelectedClips()
    except Exception:
        sel = None
    clips = []
    if isinstance(sel, dict):
        clips = list(sel.values())
    elif sel:
        clips = list(sel)
    # scarta eventuali timeline finite nel media pool
    clips = [c for c in clips
             if (c.GetClipProperty("Type") or "").lower() != "timeline"]
    if not clips:
        return None, "Seleziona la clip sorgente nel Media Pool, poi 'Aggiorna sorgente'."
    if len(clips) > 1:
        return clips[0], "Piu' clip selezionate: uso '%s'." % clips[0].GetName()
    return clips[0], "Sorgente: %s" % clips[0].GetName()


def refresh_source(ev=None):
    clip, msg = current_source_clip()
    itm["Src"].Text = msg
    return clip


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
    clip = refresh_source()
    if not clip:
        return
    proj = resolve.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()

    fps = clip.GetClipProperty("FPS")
    try:
        clip_frames = int(clip.GetClipProperty("Frames"))
    except (TypeError, ValueError):
        clip_frames = None

    # offset auto = ora tonda del primo IN; oppure valore manuale in secondi
    try:
        ins = [parse_srt_tc(r["in"]) for r in ROWS]
        outs = [parse_srt_tc(r["out"]) for r in ROWS]
    except ValueError as e:
        set_status(str(e))
        return
    srt_start = math.floor(min(ins) / 3600.0) * 3600.0
    manual = itm["Offset"].Text.strip()
    off = 0.0
    if manual and manual.lower() != "auto":
        try:
            off = float(manual.replace(",", "."))
        except ValueError:
            set_status("Offset non numerico: usa un numero di secondi o 'auto'.")
            return

    # timeline di destinazione
    if itm["NewTl"].Checked:
        name = itm["TlName"].Text.strip() or "MakeACut_Assembly"
        tl = mp.CreateEmptyTimeline(name)
        n = 2
        while tl is None and n < 100:
            tl = mp.CreateEmptyTimeline("%s (%d)" % (name, n))
            n += 1
        if tl is None:
            set_status("Impossibile creare la nuova timeline.")
            return
    else:
        tl = proj.GetCurrentTimeline()
        if tl is None:
            set_status("Nessuna timeline aperta. Aprine una o spunta 'Crea nuova timeline'.")
            return
        proj.SetCurrentTimeline(tl)

    color = itm["Color"].CurrentText or "Yellow"
    ok = 0
    fails = []
    warn = 0
    tl_start = tl.GetStartFrame()
    for i, r in enumerate(ROWS):
        in_f = to_frames(ins[i] - srt_start + off, fps)
        out_f = to_frames(outs[i] - srt_start + off, fps)   # endFrame esclusivo
        if in_f < 0:
            in_f = 0
        if out_f <= in_f:
            out_f = in_f + 1
        if clip_frames and out_f > clip_frames:
            warn += 1
        info = {"mediaPoolItem": clip, "startFrame": in_f, "endFrame": out_f}
        try:
            added = mp.AppendToTimeline([info])
        except Exception as e:
            fails.append((r["name"], str(e)))
            continue
        if not added:
            fails.append((r["name"], "AppendToTimeline vuoto"))
            continue
        ok += 1
        # marker esattamente all'inizio della clip appena appesa
        try:
            item = added[0]
            frame = item.GetStart() - tl_start
            tl.AddMarker(int(frame), color, r["name"], r.get("note") or "", 1, "")
        except Exception:
            pass

    msg = "Fatto: %d/%d segmenti su '%s', marker %s." % (ok, len(ROWS), tl.GetName(), color)
    if warn:
        msg += " %d oltre la durata clip (troncati)." % warn
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

<p align="center">
  <img src="resolve_script/plutonic_logo.png" width="170" alt="Plutonic">
</p>

<h1 align="center">PlutoniCut</h1>

<p align="center"><em>Rough cut di interviste in DaVinci Resolve, guidato dai timecode.</em></p>

<p align="center">
  <img src="https://img.shields.io/badge/DaVinci%20Resolve-Studio-7c3aed">
  <img src="https://img.shields.io/badge/Python-3-7c3aed">
  <img src="https://img.shields.io/badge/dipendenze-zero-7c3aed">
  <img src="https://img.shields.io/badge/by-Plutonic%20Studio-a78bfa">
</p>

---

## Cos'è

**PlutoniCut** trasforma una lista di segmenti (nome + timecode SRT di *in* e *out*)
in un **montaggio di sgrossatura** già pronto: crea una nuova timeline con solo i
pezzi da tenere, nell'ordine che decidi tu, e mette un **marker di capitolo**
colorato all'inizio di ognuno così ritrovi al volo ogni blocco.

È pensato per chi lavora con interviste lunghe e vuole saltare la noia di cercare
a mano ogni *in/out*: prepari i tagli una volta (a mano, da un cut.json o da una
trascrizione SRT) e PlutoniCut li applica in un colpo solo.

Due modi di usarlo, con **logica identica**:

- **Pannello dentro Resolve** — `PlutoniCut` compare in *Workspace ▸ Scripts ▸
  Utility*. Apri una tabella, incolli gli IN/OUT, premi e il montaggio appare sulla
  timeline aperta prendendo come sorgente la clip selezionata nel Media Pool.
- **Riga di comando** — lo stesso motore come CLI Python, con modalità *dry-run*,
  verifica contro l'SRT e un fallback che genera lo script per la Console interna.

---

## Due temi

Col pulsante in alto a destra passi tra **Scuro — Plutonic Studio** (viola) e
**Chiaro — Plutonic Labs** (paper + gradiente tramonto). La scelta è ricordata e
condivisa con BeaTonic.

---

## Requisiti

- **DaVinci Resolve Studio** (lo scripting non è disponibile nella versione gratuita).
- **Python 3** installato (serve solo per la modalità a riga di comando; il pannello
  usa il Python interno di Resolve).
- **Nessuna** dipendenza esterna da installare.

---

## Installazione (il pannello dentro Resolve)

1. **Scarica** questo repository: pulsante verde **Code ▸ Download ZIP**, poi estrai.
   *(Oppure, se usi GitHub Desktop: Code ▸ Open with GitHub Desktop.)*
2. Apri la cartella **degli script Utility** di Resolve:
   - **Windows:** `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`
   - **macOS:** `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility`

   *(Su Windows incolla il percorso nella barra di Esplora File e premi Invio. Se la
   cartella `Utility` non c'è, creala.)*
3. **Copia dentro quella cartella** due file presi da `resolve_script/`:
   - `PlutoniCut.py`
   - `plutonic_logo.png`  ← serve per mostrare il logo nel pannello.
4. **Avvia (o riavvia) DaVinci Resolve.**
5. Trovi il plugin in **Workspace ▸ Scripts ▸ Utility ▸ PlutoniCut**.

---

## Come si usa (pannello)

1. Apri la **timeline** su cui vuoi lavorare e seleziona nel **Media Pool** la clip
   sorgente (l'intervista).
2. Lancia **PlutoniCut**: si apre la tabella dei tagli.
3. Inserisci per ogni riga **Nome**, **IN** e **OUT** in timecode SRT
   (es. `01:00:37,958`). Puoi anche **caricare un cut.json** per riempirla.
4. Premi per applicare: nasce la timeline con i segmenti in ordine e i marker di
   capitolo.

### Riga di comando

```bash
# Anteprima senza toccare Resolve
python makeacut.py run examples/cuts.example.json --dry-run

# Esecuzione live: crea la timeline e appende i segmenti
python makeacut.py run examples/cuts.example.json

# Fallback: genera lo script per la Console interna (Workspace ▸ Console ▸ Py3)
python makeacut.py run examples/cuts.example.json --console-out cut_console.py

# Verifica i tagli contro l'SRT prima di eseguire
python makeacut.py check examples/cuts.example.json --srt examples/sample.srt
```

| Flag | Effetto |
|---|---|
| `--dry-run` | Stampa il piano, non modifica la timeline |
| `--offset SEC` | Offset manuale in secondi (sovrascrive `srt_offset`) |
| `--fps N` | fps di fallback se la clip non è leggibile (default 25) |
| `--marker-color C` | Colore dei marker (default `Yellow`) |
| `--console-out FILE` | Scrive lo script Console invece di eseguire |
| `--resolve-lib PATH` | Path esplicito a `fusionscript.dll`/`.so` |

### Il file cuts.json

```json
{
  "timeline_name": "EP2_ROUGH_ASSEMBLY",
  "clip_hint": "INTERVIEW_EP2",
  "srt_offset": "auto",
  "segments": [
    {"name": "01 origini", "in": "01:00:37,958", "out": "01:01:13,250", "note": "attacco"}
  ]
}
```

- `srt_offset`: `"auto"` deduce l'origine dell'SRT dall'ora tonda del primo segmento
  (es. `01:00:00`) e la incrocia con lo Start TC reale della clip. Un numero = offset
  manuale in secondi.
- `note` (opzionale) finisce nella nota del marker.

---

## Note tecniche

- Timecode SRT con virgola per i millisecondi (`01:00:37,958`); accettato anche il punto.
- fps NTSC (23.976 / 29.97 / 59.94) normalizzati alla frazione esatta per non perdere
  frame sulle clip lunghe.
- Trova la clip sorgente dalla **selezione** nel Media Pool oppure con un **fuzzy match**
  ricorsivo su `clip_hint`.

---

## Licenza

Software proprietario — © Matteo Congedo / Plutonic Studio. Tutti i diritti riservati.
Vedi [LICENSE](LICENSE).

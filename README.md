# MakeACut

Automazione del *rough cut* di interviste in DaVinci Resolve.
Da una lista di segmenti (nome + timecode SRT in/out) costruisce una **nuova
timeline** con i pezzi da tenere, in ordine, e un **marker di capitolo** colorato
all'inizio di ognuno.

- Python 3, **zero dipendenze esterne**.
- Due modalità: **esterna** (API di scripting, richiede Resolve aperto) e
  **Console** (genera uno script da incollare in Workspace > Console > Py3),
  con **logica identica**.

## Uso

```bash
# Anteprima senza modificare Resolve (legge fps/StartTC in sola lettura se disponibile)
python makeacut.py run examples/cuts.example.json --dry-run

# Esecuzione live: crea la timeline e appende i segmenti
python makeacut.py run examples/cuts.example.json

# Fallback: genera lo script per la Console interna
python makeacut.py run examples/cuts.example.json --console-out cut_console.py

# Verifica i tagli contro l'SRT prima di eseguire
python makeacut.py check examples/cuts.example.json --srt examples/sample.srt
```

### Opzioni principali di `run`
| Flag | Effetto |
|---|---|
| `--dry-run` | Stampa il piano, non modifica la timeline |
| `--offset SEC` | Offset manuale in secondi (sovrascrive `srt_offset`) |
| `--fps N` | fps di fallback se la clip non è leggibile (default 25) |
| `--marker-color C` | Colore marker (default `Yellow`) |
| `--console-out FILE` | Scrive lo script Console invece di eseguire |
| `--resolve-lib PATH` | Path esplicito a `fusionscript.dll`/`.so` |

## cuts.json

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

- `srt_offset`: `"auto"` deduce l'origine dell'SRT dall'ora tonda del primo
  segmento (es. `01:00:00`) e la incrocia con lo Start TC reale della clip, che
  finisce nel log come verifica. Un numero = offset manuale in secondi.
- `note` (opzionale) finisce nella nota del marker.

## Come trova la clip
1. Selezione corrente nel media pool (se l'API la espone).
2. Fuzzy match su `clip_hint`, ricorsivo in tutte le sottocartelle.

## Note tecniche
- Timecode SRT con virgola per i ms (`01:00:37,958`), accettato anche il punto.
- fps NTSC (23.976 / 29.97 / 59.94) normalizzati alla frazione esatta per non
  perdere frame su clip lunghe.
- `AppendToTimeline` usa `endFrame` **inclusivo** (lunghezza = end − start + 1);
  l'`out` SRT, che è un confine esclusivo, è mappato a `out_frame − 1`.

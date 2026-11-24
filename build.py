"""Costruisce resolve_scripts/PlutoniCut.py: la casa + gli strumenti inclusi (compressi).

Per aggiungere uno strumento: una voce in TOOLS (il suo .py deve avere setup(),
CMDS, STATE, start_server() e la pagina con la modalita' ?embed, come gli altri).
    python build.py
"""
import base64
import json
import os
import re
import zlib

D = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(D)
VERSION = "1.0"

TOOLS = [
    {"id": "nosilence", "name": "NoSilence", "file": "@nosilence",
     "tag": "togli i silenzi", "color": "linear-gradient(135deg, #ef4444, #f97316)",
     "desc": "Ascolta la voce e toglie le pause tra le parole: soglia, ritmo e margini regolabili con "
             "anteprima. Video e audio vengono tagliati insieme, in una timeline nuova.",
     "intro": "Toglie i silenzi tra le parole: dove la voce tace, NoSilence taglia audio E video insieme, "
              "senza toccare la timeline originale.",
     "steps": ["Apri la timeline in Resolve e scegli le tracce audio con la voce (e se vuoi solo un tratto).",
               "Premi Analizza: vedi la voce, la soglia del rumore e i silenzi che verranno tolti.",
               "Regola soglia (o «Trova da sola»), ritmo e margini guardando l'anteprima.",
               "Scegli cosa fare dei silenzi (rimuovi, taglia e lascia, muta…) e premi «Togli i silenzi»."]},
    {"id": "plutonicamera", "name": "PlutoniCamera", "file": "@plutonicamera",
     "tag": "cambi camera automatici", "color": "linear-gradient(135deg, #0ea5e9, #6366f1)",
     "desc": "Più camere in sync (es. un'intervista): alterna gli angoli sulle pause, su chi parla e "
             "sui momenti di enfasi, con priorità e durata minima. Tocca solo il video.",
     "intro": "Monta da solo i cambi camera di un'intervista girata con più camere in sync: tu decidi "
              "le regole, PlutoniCamera alterna gli angoli. L'audio resta com'è.",
     "steps": ["Apri la timeline con le camere in sync, una per traccia video.",
               "Scegli le tracce camera, la loro priorità e (se c'è) il microfono di chi è inquadrato.",
               "Scegli quando cambiare (pause, chi parla, enfasi, tagli esistenti) e la durata minima.",
               "Premi «Monta i cambi»: i cambi arrivano su una traccia video nuova in cima, l'audio non si tocca."]},
    {"id": "zoomtonic", "name": "ZoomTonic", "file": "@zoomtonic",
     "tag": "zoom dinamici", "color": "linear-gradient(135deg, #22c55e, #14b8a6)",
     "desc": "Zoom automatici sulle clip di una traccia video: su tagli, inizi di frase ed enfasi della "
             "voce, con ritmo, intensità, punto d'ancoraggio e stile (scatto, morbido, rapido).",
     "intro": "Aggiunge zoom dinamici alle clip di una traccia video, dove vuoi tu e come vuoi tu: "
              "solo il video cambia, l'audio resta com'è.",
     "steps": ["Apri la timeline e scegli la traccia video su cui mettere gli zoom.",
               "Scegli il ritmo, quando zoomare (tagli, inizio frase, enfasi) e quanto (min e max).",
               "Clicca sull'immagine il punto da tenere al centro e scegli gli stili di zoom.",
               "Premi «Applica gli zoom»: puoi toglierli in ogni momento con «Togli gli zoom»."]},
    {"id": "beatonic", "name": "BeaTonic", "file": r"BeatCut\resolve_scripts\BeaTonic.py",
     "tag": "montaggio a tempo di musica", "color": "linear-gradient(135deg, #7c3aed, #c026d3)",
     "desc": "Clip e canzone dal Media Pool: BeaTonic trova scene e beat e ti propone quattro "
             "montaggi a tempo, da ritoccare nell'editor dei marker o montare subito.",
     "intro": "Monta in automatico le tue clip a tempo di musica: ogni stacco cade sul beat, "
              "senza ripetere lo stesso pezzo di video.",
     "steps": ["Nel Media Pool seleziona le clip video e la canzone.",
               "Scegli durata (o canzone intera), ritmo, formato e ordine. Se vuoi, adatta la "
               "canzone alla durata: resta la stessa, solo più corta o più lunga.",
               "Premi Analizza: trovi quattro montaggi già pronti con le tue impostazioni.",
               "Aprili nell'editor dei marker per ritoccare i tagli, oppure Monta: nasce la timeline."]},
    {"id": "plutonicedit", "name": "PlutonicEdit", "file": r"MakeACut\resolve_script\PlutonicEdit.py",
     "tag": "sgrossatura di interviste", "color": "linear-gradient(135deg, #F7A541, #EE3A5B)",
     "desc": "Scrivi gli in/out dei pezzi da tenere (o prendili dalla playhead) e PlutonicEdit "
             "costruisce la timeline montata, multicam e audio compresi. L'originale non si tocca.",
     "intro": "Tiene solo i pezzi dell'intervista che ti servono e taglia il resto, in una timeline "
              "nuova: l'originale resta intatta.",
     "steps": ["Apri in Resolve la timeline dell'intervista (anche con più camere e audio separato).",
               "Scrivi in e out dei pezzi da tenere: ◎ li prende dalla playhead, oppure trascina un cut.json.",
               "Premi Monta: nasce una timeline con solo quei pezzi, tutte le tracce allineate e un "
               "marker per ogni capitolo."]},
    {"id": "subfinder", "name": "SubFinder", "file": r"SubFinder\resolve_scripts\SubFinder.py",
     "tag": "cerca nei sottotitoli", "color": "linear-gradient(135deg, #14b8a6, #3b82f6)",
     "desc": "Scrivi una parola e la playhead salta al sottotitolo che la contiene; con Invio e le "
             "frecce scorri tutti i punti in cui compare.",
     "intro": "Trova una parola nei sottotitoli della timeline e porta lì la playhead: il «trova» "
              "per chi monta interviste e parlato.",
     "steps": ["Apri in Resolve una timeline con una traccia sottotitoli.",
               "Scrivi la parola: la playhead salta al primo sottotitolo che la contiene.",
               "Invio o ↓ per il successivo, ↑ per il precedente, clic su una riga per andarci."]},
]


# strumenti nuovi, costruiti qui da src/tools (ossatura comune in src/common)
NEW_TOOLS = [("nosilence", "NoSilence"), ("zoomtonic", "ZoomTonic"), ("plutonicamera", "PlutoniCamera")]


def rd(p):
    return open(p, encoding="utf-8").read()


def build_tool(key, name):
    """src/tools/<key>_tpl.py + <key>.html + src/common -> strumenti/<Name>.py (funziona anche da solo)."""
    C = os.path.join(D, "src", "common")
    page = (rd(os.path.join(D, "src", "tools", key + ".html"))
            .replace("/*@@CSS@@*/", rd(os.path.join(C, "base.css")) + rd(os.path.join(C, "ui.css")))
            .replace("//@@JS@@", rd(os.path.join(C, "base.js")))
            .replace("//@@UIJS@@", rd(os.path.join(C, "ui.js"))))
    assert '"""' not in page
    py = (rd(os.path.join(D, "src", "tools", key + "_tpl.py"))
          .replace("#@@CORE@@", rd(os.path.join(C, "core.py")).rstrip("\n"))
          .replace("#@@MEDIA@@", rd(os.path.join(C, "media.py")).rstrip("\n"))
          .replace("@@PAGE@@", page))
    out = os.path.join(D, "strumenti", name + ".py")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8", newline="\n").write(py)
    return out


def between(s, a, b, incl_b=False):
    i = s.index(a)
    j = s.index(b, i)
    return s[i:j + (len(b) if incl_b else 0)]


# ossatura comune (server, finestra, aspetti, sempre davanti)
C = os.path.join(D, "src", "common")
common_py = rd(os.path.join(C, "core.py"))
css = rd(os.path.join(C, "base.css"))
js = rd(os.path.join(C, "base.js"))
built = {}
for key, name in NEW_TOOLS:
    if os.path.exists(os.path.join(D, "src", "tools", key + "_tpl.py")):
        built[key] = build_tool(key, name)
        print("ok", built[key])

hub = rd(os.path.join(D, "src", "hub.html")).replace("/*@@CSS@@*/", css).replace("//@@JS@@", js)
assert '"""' not in hub
sources, tools = {}, []
for t in TOOLS:
    if t["file"].startswith("@") and t["file"][1:] not in built:
        continue                                  # strumento nuovo non ancora scritto
    src = rd(built[t["file"][1:]] if t["file"].startswith("@") else os.path.join(HOME, t["file"]))
    assert src.rstrip().endswith("main()") and "def setup():" in src, t["name"]
    sources[t["id"]] = base64.b64encode(zlib.compress(src.encode("utf-8"), 9)).decode("ascii")
    tools.append({k: v for k, v in t.items() if k != "file"})

src_lines = "{\n" + "".join('    "%s": (\n%s),\n' % (k, "\n".join('        "%s"' % v[i:i + 96] for i in range(0, len(v), 96)))
                            for k, v in sources.items()) + "}"
py = rd(os.path.join(D, "src", "hub_tpl.py"))
py = (py.replace("#@@COMMON@@", common_py)
        .replace("@@VERSION@@", VERSION)
        .replace("@@TOOLS@@", json.dumps(tools, ensure_ascii=False, indent=4))
        .replace("@@SOURCES@@", src_lines)
        .replace("@@PAGE@@", hub))
out = os.path.join(D, "resolve_scripts", "PlutoniCut.py")
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8", newline="\n").write(py)
print("ok", out, "%d KB" % (len(py) // 1024))

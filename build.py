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


def rd(p):
    return open(p, encoding="utf-8").read()


def between(s, a, b, incl_b=False):
    i = s.index(a)
    j = s.index(b, i)
    return s[i:j + (len(b) if incl_b else 0)]


# ossatura comune (server, finestra, aspetti, sempre davanti) presa da SubFinder
sf = rd(os.path.join(HOME, r"SubFinder\resolve_scripts\SubFinder.py"))
SEP = "# " + "-" * 74 + "\n"
common_py = between(sf, SEP + "# Finestra a parte", SEP + "# Lettura sottotitoli").rstrip() + "\n"
page = sf[sf.index('PAGE_HTML = r"""'):]
css = between(page, "<style>\n", "@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }", True)[len("<style>\n"):]
js = between(page, '"use strict";\n', "const BUSY")[len('"use strict";\n'):]

hub = rd(os.path.join(D, "src", "hub.html")).replace("/*@@CSS@@*/", css).replace("//@@JS@@", js)
assert '"""' not in hub
sources, tools = {}, []
for t in TOOLS:
    src = rd(os.path.join(HOME, t["file"]))
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

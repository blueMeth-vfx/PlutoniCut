"""MakeACut command-line interface."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import __version__
from .cutsfile import load_cuts
from .planner import build_plan
from .timecode import frames_to_tc, normalize_fps, parse_frame_tc, seconds_to_clock


def _log(msg: str = ""):
    print(msg, flush=True)


def _resolve_clip_props(clip_hint: str, lib_override: Optional[str]):
    """Try to connect and locate the clip. Returns (props|None, clip|None, media_pool, project, resolve, lines)."""
    from .resolve_env import connect, ResolveUnavailable
    from .resolve_ops import find_clip, read_clip_props

    try:
        resolve, info = connect(lib_override)
    except ResolveUnavailable as exc:
        return None, None, None, None, None, [f"Resolve non raggiungibile: {exc}"]

    lines = [f"Connesso a Resolve (lib={info['lib']})"]
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        return None, None, None, None, resolve, lines + ["Nessun progetto aperto"]
    media_pool = project.GetMediaPool()
    clip, find_log = find_clip(project, media_pool, clip_hint)
    lines += find_log
    if not clip:
        return None, None, media_pool, project, resolve, lines
    props = read_clip_props(clip)
    return props, clip, media_pool, project, resolve, lines


def _print_plan(plan, fps_source: str):
    _log(f"  fps: {plan.fps:.6g}  ({fps_source})")
    _log(f"  inizio SRT dedotto: {seconds_to_clock(plan.srt_start_sec)}  (= {plan.srt_start_sec/3600:.2f}h)")
    if plan.offset_sec:
        _log(f"  offset manuale applicato: {plan.offset_sec:+.3f}s")
    for w in plan.warnings:
        _log(f"  ! {w}")
    _log("")
    _log(f"  {'#':>2}  {'segmento':<26} {'src in':>12} {'src out':>12}  {'frame in':>9} {'frame out':>9}")
    for i, it in enumerate(plan.items, 1):
        _log(
            f"  {i:>2}  {it.name[:26]:<26} "
            f"{frames_to_tc(round(it.src_in_sec*plan.fps), plan.fps):>12} "
            f"{frames_to_tc(round(it.src_out_sec*plan.fps), plan.fps):>12}  "
            f"{it.start_frame:>9} {it.end_frame:>9}"
        )
    _log("")
    _log(f"  segmenti: {len(plan.items)}   durata parlato totale: {seconds_to_clock(plan.total_spoken_sec)}")


def cmd_run(args) -> int:
    cuts = load_cuts(args.cuts)
    _log(f"MakeACut {__version__} :: '{cuts.timeline_name}'  ({len(cuts.segments)} segmenti)")
    _log("")

    # Read real fps/StartTC from the clip when Resolve is reachable.
    props = clip = media_pool = project = resolve = None
    fps = None
    clip_start_sec = None
    fps_source = "flag --fps"

    want_live = not args.console_out  # console-out never touches the timeline
    if want_live or not args.dry_run:
        props, clip, media_pool, project, resolve, lines = _resolve_clip_props(
            cuts.clip_hint, args.resolve_lib
        )
        for ln in lines:
            _log(f"  {ln}")
        if props:
            fps = props["fps"]
            fps_source = f"clip '{props['name']}'"
            try:
                clip_start_sec = parse_frame_tc(props["start_tc"], fps)
            except Exception:
                clip_start_sec = None
            _log(f"  clip: {props['name']}  fps={props['fps']}  StartTC={props['start_tc']}")

    if fps is None:
        fps = args.fps
        _log(f"  ! fps dalla clip non disponibile: uso --fps {fps}")

    manual = args.offset
    plan = build_plan(
        cuts.segments, fps, cuts.srt_offset,
        clip_start_sec=clip_start_sec, manual_offset_sec=manual,
        clip_frames=(props.get("frames") if props else None),
    )

    _log("")
    _print_plan(plan, fps_source)

    # --- Console script output --------------------------------------------
    if args.console_out:
        from .console_script import generate_console_script
        script = generate_console_script(
            plan, cuts.timeline_name, cuts.clip_hint, args.marker_color
        )
        with open(args.console_out, "w", encoding="utf-8") as fh:
            fh.write(script)
        _log("")
        _log(f"Script per la Console scritto in: {args.console_out}")
        _log("Aprilo, copia tutto, incolla in Workspace > Console > Py3 e premi Invio.")
        return 0

    # --- Dry run -----------------------------------------------------------
    if args.dry_run:
        _log("")
        _log("[dry-run] nessuna modifica a Resolve.")
        return 0

    # --- Live execution ----------------------------------------------------
    if not (clip and media_pool):
        _log("")
        _log("Impossibile eseguire live: clip o progetto non disponibili.")
        _log("Suggerimento: genera lo script Console con --console-out cut.py")
        return 2

    from .resolve_ops import create_unique_timeline, append_plan, add_chapter_markers

    timeline, final_name = create_unique_timeline(media_pool, cuts.timeline_name)
    _log("")
    _log(f"Timeline creata: '{final_name}'")
    results = append_plan(media_pool, clip, plan)
    ok = sum(1 for _, good, _ in results if good)
    for name, good, detail in results:
        if not good:
            _log(f"  FALLITO: {name} -> {detail}")
    added = add_chapter_markers(timeline, plan, args.marker_color)

    _log("")
    _log(f"Fatto: {ok}/{len(results)} segmenti in timeline, {added} marker {args.marker_color}.")
    _log(f"Durata parlato totale: {seconds_to_clock(plan.total_spoken_sec)}")
    return 0 if ok == len(results) else 1


def cmd_check(args) -> int:
    from .srt import parse_srt, check_segment_boundaries

    cuts = load_cuts(args.cuts)
    cues = parse_srt(args.srt)
    _log(f"MakeACut check :: {len(cuts.segments)} segmenti vs {len(cues)} sottotitoli")
    _log("")
    tol = args.tolerance
    problems = 0
    for i, seg in enumerate(cuts.segments, 1):
        bc = check_segment_boundaries(cues, seg.name, seg.in_sec, seg.out_sec)
        flag_in = "" if bc.in_delta is not None and abs(bc.in_delta) <= tol else "  <-- IN fuori confine"
        flag_out = "" if bc.out_delta is not None and abs(bc.out_delta) <= tol else "  <-- OUT fuori confine"
        if flag_in or flag_out:
            problems += 1
        _log(f"{i:>2}. {seg.name}")
        _log(f"     IN  ({seg.tc_in})  d={bc.in_delta:+.3f}s{flag_in}")
        _log(f"         apre: \"{bc.open_line[:70]}\"")
        _log(f"     OUT ({seg.tc_out})  d={bc.out_delta:+.3f}s{flag_out}")
        _log(f"         chiude: \"{bc.close_line[:70]}\"")
        _log("")
    _log(f"Riepilogo: {len(cuts.segments)-problems}/{len(cuts.segments)} segmenti allineati (tol {tol}s).")
    return 0 if problems == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="makeacut", description="Rough cut automation per DaVinci Resolve.")
    p.add_argument("--version", action="version", version=f"MakeACut {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Costruisce la timeline dai tagli.")
    r.add_argument("cuts", help="path a cuts.json")
    r.add_argument("--dry-run", action="store_true", help="stampa il piano senza toccare Resolve")
    r.add_argument("--offset", type=float, default=None, help="offset manuale in secondi (sovrascrive srt_offset)")
    r.add_argument("--fps", type=float, default=25.0, help="fps di fallback se la clip non e' leggibile (default 25)")
    r.add_argument("--marker-color", default="Yellow", help="colore marker capitolo (default Yellow)")
    r.add_argument("--console-out", metavar="FILE", help="scrive lo script per la Console invece di eseguire")
    r.add_argument("--resolve-lib", help="path esplicito a fusionscript.dll/.so")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("check", help="Valida i tagli contro un file SRT.")
    c.add_argument("cuts", help="path a cuts.json")
    c.add_argument("--srt", required=True, help="path al file .srt")
    c.add_argument("--tolerance", type=float, default=0.15, help="tolleranza confine in secondi (default 0.15)")
    c.set_defaults(func=cmd_check)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        _log(f"Errore: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Locate the DaVinciResolveScript module and connect to a running Resolve.

Handles macOS and Windows default paths plus the RESOLVE_SCRIPT_API /
RESOLVE_SCRIPT_LIB / PYTHONPATH env vars. Also probes a few non-standard install
dirs (Resolve can live on any drive) and honours an explicit --resolve-lib path.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional


class ResolveUnavailable(RuntimeError):
    """Raised when the external scripting API can't be reached."""


def _candidate_api_dirs() -> List[str]:
    if sys.platform.startswith("darwin"):
        return [
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
        ]
    if sys.platform.startswith("win"):
        pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return [
            os.path.join(pd, r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"),
        ]
    # linux
    return [
        "/opt/resolve/Developer/Scripting",
        "/home/resolve/Developer/Scripting",
    ]


def _candidate_libs() -> List[str]:
    if sys.platform.startswith("darwin"):
        return [
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
        ]
    if sys.platform.startswith("win"):
        return [
            r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
            r"S:\DaVinciStudio\fusionscript.dll",  # detected non-standard install
        ]
    return ["/opt/resolve/libs/Fusion/fusionscript.so"]


def _first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def prepare_environment(lib_override: Optional[str] = None) -> dict:
    """Set env vars + sys.path so `import DaVinciResolveScript` works.

    Returns a dict describing what was used, for logging. Idempotent.
    """
    api_dir = os.environ.get("RESOLVE_SCRIPT_API") or _first_existing(_candidate_api_dirs())
    if api_dir:
        os.environ["RESOLVE_SCRIPT_API"] = api_dir
        modules = os.path.join(api_dir, "Modules")
        if modules not in sys.path:
            sys.path.append(modules)

    lib = lib_override or os.environ.get("RESOLVE_SCRIPT_LIB") or _first_existing(_candidate_libs())
    if lib:
        os.environ["RESOLVE_SCRIPT_LIB"] = lib

    return {"api_dir": api_dir, "lib": lib}


def connect(lib_override: Optional[str] = None):
    """Return a connected `resolve` object, or raise ResolveUnavailable."""
    info = prepare_environment(lib_override)
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except Exception as exc:  # ImportError or dll load failure
        raise ResolveUnavailable(
            "Modulo DaVinciResolveScript non importabile "
            f"(api_dir={info['api_dir']}, lib={info['lib']}): {exc}"
        ) from exc

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveUnavailable(
            "scriptapp('Resolve') ha restituito None. "
            "Resolve e' aperto? In Preferences > System > General, "
            "'External scripting using' e' abilitato (Local)?"
        )
    return resolve, info

#!/usr/bin/env python3
"""Launcher senza installazione: python makeacut.py run cuts.json ..."""
import sys

from makeacut.cli import main

if __name__ == "__main__":
    sys.exit(main())

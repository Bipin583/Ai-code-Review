"""Launcher: ``streamlit run dashboard/app.py``.

Adds ``src`` to the import path so the dashboard also runs from a fresh clone,
before ``pip install -e .``.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reviewbot.dashboard.app import main  # noqa: E402

main()

"""Locate the rules engine regardless of how the bot was started.

The bot must never grow its own copy of the scoring rules - that is how a
live competition starts disagreeing with its own published rulebook. We load
../engine/scoring.py explicitly so there is exactly one source of truth.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
CANDIDATES = [HERE.parent / "engine" / "scoring.py", HERE / "engine" / "scoring.py"]


def load_scoring():
    if "hub_scoring" in sys.modules:
        return sys.modules["hub_scoring"]
    for path in CANDIDATES:
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location("hub_scoring", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hub_scoring"] = mod          # required for dataclass introspection
        spec.loader.exec_module(mod)
        mod.__path__ = str(path)                   # so errors can name the file
        return mod
    raise FileNotFoundError(
        "Could not find engine/scoring.py. Expected one of:\n  "
        + "\n  ".join(str(c) for c in CANDIDATES)
        + "\nKeep the repo layout (bot/ next to engine/) or set the engine path there.")


def load_db():
    sys.path.insert(0, str(HERE))
    import db                                     # noqa: WPS433
    return db

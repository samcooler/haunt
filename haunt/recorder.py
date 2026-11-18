"""Records every bus message to a JSONL file per run, so any session can be read
afterwards (by you or Claude) and, later, replayed against a changed show."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import topics
from .bus import Bus


class Recorder:
    def __init__(self, bus: Bus, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / time.strftime("%Y-%m-%d_%H%M%S.jsonl")
        self._f = self.path.open("a", buffering=1)
        bus.subscribe(f"{topics.ROOT}/#", self._on)

    def _on(self, topic: str, payload: Any) -> None:
        self._f.write(json.dumps({"t": round(time.time(), 3), "topic": topic, "p": payload},
                                 separators=(",", ":")) + "\n")

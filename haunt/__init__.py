"""haunt: a runtime for interactive, immersive art.

A piece is a folder (devices.yaml, params.yaml, show.py, media/). The hub loads it,
talks to every node over MQTT, and hot-reloads it when a file changes.
"""

from .show import Show, Event

__all__ = ["Show", "Event"]
__version__ = "0.1.0"

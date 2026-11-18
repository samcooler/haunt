"""Loading a piece folder: devices.yaml, params.yaml, show.py, media/."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .show import Show


class PieceError(Exception):
    pass


class Piece:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_dir():
            raise PieceError(f"no piece folder at {self.path}")
        self.name = self.path.name

    @property
    def media(self) -> Path:
        return self.path / "media"

    def watched_files(self) -> list[Path]:
        files = [p for p in self.path.glob("*.py")]
        files += [self.path / "devices.yaml", self.path / "params.yaml"]
        return files

    def mtimes(self) -> dict[str, float]:
        out = {}
        for f in self.watched_files():
            try:
                out[str(f)] = f.stat().st_mtime
            except FileNotFoundError:
                pass
        return out

    def load_devices(self) -> dict[str, dict]:
        data = _yaml(self.path / "devices.yaml") or {}
        nodes = data.get("nodes") or {}
        if not isinstance(nodes, dict):
            raise PieceError("devices.yaml: 'nodes' must be a mapping of name -> node")
        for name, node in nodes.items():
            if node is None:
                nodes[name] = {}
            elif not isinstance(node, dict):
                raise PieceError(f"devices.yaml: node {name!r} must be a mapping")
            if "." in name or "/" in name:
                raise PieceError(f"devices.yaml: node name {name!r} cannot contain '.' or '/'")
        return nodes

    def load_param_specs(self) -> dict[str, dict]:
        data = _yaml(self.path / "params.yaml") or {}
        specs = {}
        for name, v in data.items():
            spec = dict(v) if isinstance(v, dict) else {"value": v}
            if "value" not in spec:
                raise PieceError(f"params.yaml: {name} needs a value")
            spec.setdefault("type", _infer_type(spec["value"]))
            specs[name] = spec
        return specs

    def load_show(self) -> Show:
        """Execute show.py fresh (not from the import cache) and return its `show`."""
        file = self.path / "show.py"
        if not file.exists():
            raise PieceError(f"{file} not found")
        # Let show.py import helper modules that sit beside it.
        if str(self.path) not in sys.path:
            sys.path.insert(0, str(self.path))
        for mod in [m for m, v in sys.modules.items()
                    if getattr(v, "__file__", None) and Path(v.__file__).parent == self.path]:
            del sys.modules[mod]
        spec = importlib.util.spec_from_file_location(f"haunt_piece_{self.name}", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # errors propagate to the caller, which keeps the old show
        show = getattr(module, "show", None)
        if not isinstance(show, Show):
            raise PieceError("show.py must define `show = Show(...)`")
        if not show.states:
            raise PieceError("show.py defines no states")
        if show.initial not in show.states:
            raise PieceError(f"initial state {show.initial!r} is not defined")
        for st in show.states.values():
            if st.then and st.then not in show.states:
                raise PieceError(f"state {st.name}: then={st.then!r} is not a state")
        return show


def _yaml(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PieceError(f"{path.name}: {e}") from e


def _infer_type(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str) and v.startswith("#") and len(v) in (4, 7):
        return "color"
    return "text"


class Params:
    """Param values: defaults from params.yaml, live overrides saved beside the logs.

    Read them in a show as `s.param.name` or `p.name` inside an output lambda.
    """

    def __init__(self, store: Path | None = None) -> None:
        self.specs: dict[str, dict] = {}
        self.overrides: dict[str, Any] = {}
        self.store = store
        if store and store.exists():
            try:
                self.overrides = json.loads(store.read_text())
            except (OSError, json.JSONDecodeError):
                self.overrides = {}

    def set_specs(self, specs: dict[str, dict]) -> None:
        self.specs = specs

    def get(self, name: str) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        if name in self.specs:
            return self.specs[name]["value"]
        raise AttributeError(f"no param {name!r} (add it to params.yaml)")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in ("specs", "overrides", "store"):
            raise AttributeError(name)
        return self.get(name)

    def __getitem__(self, name: str) -> Any:
        return self.get(name)

    def coerce(self, name: str, value: Any) -> Any:
        spec = self.specs.get(name)
        if spec is None:
            raise KeyError(f"no param {name!r}")
        t = spec["type"]
        if t == "bool":
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "on")
            return bool(value)
        if t in ("int", "float"):
            value = int(round(float(value))) if t == "int" else float(value)
            if "min" in spec:
                value = max(spec["min"], value)
            if "max" in spec:
                value = min(spec["max"], value)
            return value
        return str(value)

    def set(self, name: str, value: Any) -> Any:
        value = self.coerce(name, value)
        if value == self.specs[name]["value"]:
            self.overrides.pop(name, None)
        else:
            self.overrides[name] = value
        self._save()
        return value

    def reset(self, name: str | None = None) -> None:
        if name is None:
            self.overrides.clear()
        else:
            self.overrides.pop(name, None)
        self._save()

    def snapshot(self) -> dict[str, dict]:
        out = {}
        for name, spec in self.specs.items():
            out[name] = {**spec, "default": spec["value"], "value": self.get(name),
                         "modified": name in self.overrides}
        return out

    def _save(self) -> None:
        if self.store:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps(self.overrides, indent=1, sort_keys=True))

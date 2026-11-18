"""The show engine: runs a piece's show against the bus.

What it guarantees, so a piece does not have to:
  - a state that crashes is logged and the show falls back to the initial state;
  - every state can time out; `idle_reset` returns an untouched show to the start;
  - outputs are remembered per node and re-sent when a node comes back online;
  - outputs written as functions of params are re-sent when a param changes;
  - editing show.py, params.yaml or devices.yaml applies live; a broken edit is
    reported and the old show keeps running;
  - operators can force any state, reset, black out, or press a show's actions.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
import random
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import topics
from .bus import Bus
from .piece import Params, Piece, PieceError
from .show import Event, Show

log = logging.getLogger("haunt.engine")


class Goto(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class Vars:
    """The show's own variables: `s.vars.pulls += 1`. Survive reloads, cleared on Reset."""

    def __init__(self) -> None:
        object.__setattr__(self, "_d", {})

    def __getattr__(self, name: str) -> Any:
        try:
            return self._d[name]
        except KeyError:
            raise AttributeError(f"no variable {name!r}; set it first (e.g. in @show.on_reset)") from None

    def __setattr__(self, name: str, value: Any) -> None:
        self._d[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        return self._d.get(name, default)

    def clear(self) -> None:
        self._d.clear()

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._d, default=repr))


class Ctx:
    """`s` in a show: everything a state or handler can do."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine

    # -- reading --------------------------------------------------------------
    @property
    def param(self) -> Params:
        return self._e.params

    @property
    def vars(self) -> Vars:
        return self._e.vars

    @property
    def state(self) -> str | None:
        return self._e.state

    @property
    def elapsed(self) -> float:
        """Seconds since the current state was entered."""
        return time.time() - self._e.since

    def value(self, key: str, default: Any = None) -> Any:
        """Latest value of an input, e.g. s.value("ring.pull")."""
        return self._e.values.get(key, default)

    def online(self, node: str) -> bool:
        return bool(self._e.nodes.get(node, {}).get("online"))

    @property
    def devices(self) -> dict[str, dict]:
        """devices.yaml's nodes, by name: read your own keys from it (a rope's colour...)."""
        return self._e.devices

    @property
    def keep(self) -> dict:
        """Live objects the show needs across events and reloads (a game model, a
        renderer): not published like vars, cleared on Reset."""
        return self._e.keep

    def load(self, name: str) -> Any:
        """One of the piece's own YAML files (content.yaml ...), parsed; edits reload the show."""
        return self._e.load_yaml(name)

    @property
    def entries(self) -> int:
        """How many states have been entered so far. A handler doing several things in a
        row compares it before and after each to notice that the show moved on."""
        return self._e.entries

    def pick(self, *patterns: str) -> str | None:
        """A media file matching any of the globs (relative to media/, extension optional),
        at random, never the same one twice running for the same patterns."""
        return self._e.pick(patterns)

    # -- the current state's own handlers and timers: they end with the state --
    def on(self, pattern: str | list[str], fn: Callable | None = None):
        """While in this state, call fn(ev) for each matching event. Also a decorator."""
        def deco(f: Callable) -> Callable:
            for p in [pattern] if isinstance(pattern, str) else pattern:
                self._e.scoped_handlers.append((p, f))
            return f
        return deco(fn) if fn else deco

    def every(self, seconds: float | str, fn: Callable | None = None, when: Callable | None = None):
        """While in this state, call fn() every N seconds (or every <param>), if when() allows."""
        def deco(f: Callable) -> Callable:
            async def loop():
                while True:
                    await asyncio.sleep(self._e.num(seconds) or 1)
                    if when is None or when():
                        await self._e.guard(f, (), f"every {getattr(f, '__name__', '')}")
            self._e.scoped_task(loop())
            return f
        return deco(fn) if fn else deco

    def at(self, seconds: float | str, fn: Callable | None = None):
        """Call fn() once, N seconds into this state (if it is still this state)."""
        def deco(f: Callable) -> Callable:
            async def once():
                await asyncio.sleep(self._e.num(seconds) or 0)
                await self._e.guard(f, (), f"at {seconds}")
            self._e.scoped_task(once())
            return f
        return deco(fn) if fn else deco

    def idle(self, seconds: float | str, fn: Callable | None = None, pattern: str = "*"):
        """Call fn() when nothing matching `pattern` has happened for N seconds in this
        state; again every N seconds of quiet after that."""
        def deco(f: Callable) -> Callable:
            last = [time.time()]
            self._e.scoped_handlers.append((pattern, lambda ev: last.__setitem__(0, time.time())))

            async def loop():
                while True:
                    wait = last[0] + (self._e.num(seconds) or 1) - time.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        continue
                    last[0] = time.time()
                    await self._e.guard(f, (), f"idle {seconds}")
            self._e.scoped_task(loop())
            return f
        return deco(fn) if fn else deco

    # -- doing ----------------------------------------------------------------
    def out(self, node: str, output: str, persist: bool = True, **spec: Any) -> None:
        self._e.send(node, output, spec, persist)

    def leds(self, node: str, **spec: Any) -> None:
        """effect=solid|breathe|chase|rainbow|sparkle|off, color, color2, speed, brightness, fade."""
        self._e.send(node, "leds", spec, True)

    def play(self, node: str, file: str, **kw: Any) -> None:
        """Play a file from the piece's media/ on a node that has audio (or video)."""
        path = self._e.piece.media / file
        if not path.is_file():
            self._e.log(f"play {file!r} on {node}: no such file in media/", "warn")
        else:
            kw["v"] = int(path.stat().st_mtime)  # nodes cache media; a new version re-downloads
        self._e.send(node, "audio", {"file": file, **kw}, bool(kw.get("loop")))

    def stop(self, node: str) -> None:
        self._e.send(node, "audio", {"stop": True}, True)

    def restore(self, node: str) -> None:
        """Re-send a node's remembered outputs, e.g. after a one-off flash (persist=False)."""
        self._e.resend(node)

    def log(self, msg: str) -> None:
        self._e.log(msg)

    def emit(self, name: str, value: Any = None) -> None:
        """Raise an event of your own; other states and handlers can wait for it."""
        self._e.event(Event(name, value, source="show"))

    def goto(self, name: str | Callable) -> None:
        """Change state now. Inside a state, `return "name"` reads better."""
        name = name if isinstance(name, str) else name.__name__
        if asyncio.current_task() is self._e._runner:
            raise Goto(name)
        self._e.goto(name, "goto")

    async def wait(self, pattern: str | list[str], timeout: float | str | None = None,
                   where: Callable[[Event], bool] | None = None) -> Event | None:
        """Wait for an event matching a pattern ("ring.button.press", "*.pull.on").

        Returns the event, or None if `timeout` (seconds or a param name) passes first.
        """
        patterns = [pattern] if isinstance(pattern, str) else list(pattern)
        fut = asyncio.get_running_loop().create_future()
        waiter = (patterns, where, fut)
        self._e.waiters.append(waiter)
        try:
            t = self._e.num(timeout)
            if t is None:
                return await fut
            try:
                return await asyncio.wait_for(fut, t)
            except TimeoutError:
                return None
        finally:
            if waiter in self._e.waiters:
                self._e.waiters.remove(waiter)

    async def sleep(self, seconds: float | str) -> None:
        await asyncio.sleep(self._e.num(seconds) or 0)


class Engine:
    def __init__(self, piece: Piece, bus: Bus, data_dir: Path | None = None) -> None:
        self.piece = piece
        self.bus = bus
        self.data_dir = data_dir
        self.params = Params(data_dir / "params.json" if data_dir else None)
        self.vars = Vars()
        self.ctx = Ctx(self)
        self.show: Show | None = None
        self.devices: dict[str, dict] = {}
        self.state: str | None = None
        self.since = time.time()
        self.nodes: dict[str, dict] = {}
        self.values: dict[str, Any] = {}
        self.outputs: dict[tuple[str, str], dict] = {}
        self.waiters: list = []
        self.blackout = False
        self.error: str | None = None
        self.last_input = time.time()
        self._runner: asyncio.Task | None = None
        self._timers: list[asyncio.Task] = []
        self._tasks: set[asyncio.Task] = set()
        self._pubs: set[asyncio.Task] = set()
        self._assigned: set[str] = set()
        self._warned: set[str] = set()
        self._mtimes: dict[str, float] = {}
        self._vars_json = ""
        self.scoped_handlers: list[tuple[str, Callable]] = []
        self._scoped_tasks: set[asyncio.Task] = set()
        self.entries = 0            # states entered so far: a handler compares it to see a move
        self.keep: dict[str, Any] = {}
        self._yaml_cache: dict[str, tuple[float, Any]] = {}
        self._picked: dict[tuple, str] = {}
        self._media_cache: tuple[float, list[str]] = (0.0, [])

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        self.bus.subscribe(topics.status("+"), self._on_status)
        self.bus.subscribe(f"{topics.ROOT}/n/+/in/+", self._on_input)
        self.bus.subscribe(topics.assign("+"), self._on_assign)
        self.bus.subscribe(f"{topics.ROOT}/cmd/+", self._on_cmd)
        self._mtimes = self.piece.mtimes()
        self._load(first=True)

    async def run(self) -> None:
        await self.start()
        while True:
            await asyncio.sleep(0.5)
            try:
                self._housekeeping()
            except Exception:
                log.exception("housekeeping")

    def stop(self) -> None:
        for t in [self._runner, *self._timers, *self._tasks, *self._scoped_tasks]:
            if t:
                t.cancel()

    def _housekeeping(self) -> None:
        mt = self.piece.mtimes()
        if mt != self._mtimes:
            changed = {k for k in set(mt) | set(self._mtimes) if mt.get(k) != self._mtimes.get(k)}
            self._mtimes = mt
            self._load(code_changed=any(c.endswith(".py") for c in changed))
        if self.show and self.state and self.state != self.show.initial:
            idle = self.num(self.show.idle_reset)
            if idle and time.time() - max(self.last_input, self.since) > idle:
                self.log(f"nobody for {idle:.0f}s: back to {self.show.initial}")
                self.goto(self.show.initial, "idle")
        vj = json.dumps(self.vars.snapshot(), sort_keys=True)
        if vj != self._vars_json:
            self._vars_json = vj
            self._pub(topics.SHOW_VARS, self.vars.snapshot(), retain=True)

    # -- loading and hot reload ----------------------------------------------
    def _load(self, first: bool = False, code_changed: bool = True) -> None:
        try:
            specs = self.piece.load_param_specs()
            devices = self.piece.load_devices()
            show = self.piece.load_show() if (code_changed or self.show is None) else self.show
            _check_param_refs(show, specs)
        except Exception as e:
            self.error = _describe(e, self.piece.path)
            self.log(f"{'load' if first else 'reload'} failed, still running the last good show: "
                     f"{self.error}", "error")
            self._publish_state()
            return
        self.params.set_specs(specs)
        self.devices = devices
        self._publish_assignments()
        self._pub(topics.SHOW_PARAMS, self.params.snapshot(), retain=True)
        self.error = None
        if show is not self.show:
            fresh = self.show is None
            self._cancel_show_tasks()
            self.show = show
            for every in show.timers:
                self._timers.append(asyncio.create_task(self._every(every)))
            if fresh:
                self.goto(show.initial, "start", before=self._run_reset_fns)
            else:
                # Re-enter the current state so an edit to it takes effect now.
                name = self.state if self.state in show.states else show.initial
                self.log(f"reloaded show.py; re-entering {name}")
                self.goto(name, "reload")
        else:
            self.log("reloaded params/devices")
            self._publish_state()
        self.resend_all()

    def _cancel_show_tasks(self) -> None:
        self._end_state()
        for t in [self._runner, *self._timers, *self._tasks]:
            if t and t is not asyncio.current_task():
                t.cancel()
        self._runner = None
        self._timers.clear()
        self._tasks.clear()

    def _publish_assignments(self) -> None:
        ids = set()
        for name, node in self.devices.items():
            hwid = node.get("id")
            if not hwid:
                continue
            ids.add(hwid)
            config = {k: v for k, v in node.items() if k not in ("id", "sim", "kind", "note")}
            msg = {"name": name, "config": config}
            hub = self.piece.config.get("hub")   # the hub's fixed IP: a board's fallback if mDNS fails
            if hub:
                msg["hub"] = str(hub)
            self._pub(topics.assign(hwid), msg, retain=True)
        for gone in self._assigned - ids:
            self._pub(topics.assign(gone), None, retain=True)
        self._assigned = ids

    def _on_assign(self, topic: str, payload: Any) -> None:
        hwid = topic.rsplit("/", 1)[1]
        # A retained assignment from an earlier devices.yaml: clear it.
        if payload is not None and self.show is not None and hwid not in self._assigned:
            self._pub(topic, None, retain=True)

    # -- states ---------------------------------------------------------------
    # Entering a state is synchronous: the old state's handlers and timers end, and a
    # plain (non-async) state function runs right here, registering its own handlers
    # and doing its entry actions before goto() returns. So a reaction that changes
    # state mid-way through a burst of events hands the rest of the burst to the new
    # state, in order. An async state's body runs as a task (it may wait and sleep).
    def goto(self, name: str, reason: str = "", before: Callable | None = None) -> None:
        if not self.show or name not in self.show.states:
            self.log(f"no state {name!r}", "error")
            return
        if before:
            async def then():
                await before()
                self._enter(name, reason)
            self._cancel_runner()
            self._runner = asyncio.create_task(then())
            return
        self._enter(name, reason)

    def _cancel_runner(self) -> None:
        if self._runner and self._runner is not asyncio.current_task():
            self._runner.cancel()
        self._runner = None

    def _enter(self, name: str, reason: str = "", depth: int = 0) -> None:
        show = self.show
        if name not in show.states:
            self.log(f"{self.state} asked for {name!r}, which is not a state; going to {show.initial}", "error")
            name = show.initial
        self._cancel_runner()
        self._end_state()
        self.entries += 1
        entry = self.entries
        prev, self.state, self.since = self.state, name, time.time()
        self.log(f"→ {name}" + (f" ({reason})" if reason else "") + (f" from {prev}" if prev else ""))
        self._publish_state()
        spec = show.states[name]
        if inspect.iscoroutinefunction(spec.fn):
            self._runner = asyncio.create_task(self._run_async(spec))
            return
        try:
            nxt = spec.fn(self.ctx)
        except Goto as g:
            nxt = g.name
        except Exception as e:
            self.error = f"state {name} crashed: {_describe(e, self.piece.path)}"
            self.log(self.error, "error")
            if self.entries == entry:
                self._runner = asyncio.create_task(self._after_crash(name))
            return
        if self.entries != entry:
            return  # the entry itself moved the show on
        if nxt is not None:
            if depth > 20:
                self.log(f"states keep moving on at once ({name}); stopping at {name}", "error")
            else:
                return self._enter(nxt if isinstance(nxt, str) else nxt.__name__, "", depth + 1)
        self._runner = asyncio.create_task(self._hold(spec))

    def _end_state(self) -> None:
        """Drop the state's own handlers and timers, now, so nothing of it outlives it."""
        self.scoped_handlers.clear()
        for t in list(self._scoped_tasks):
            if t is not asyncio.current_task():
                t.cancel()
        self._scoped_tasks.clear()

    def scoped_task(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._scoped_tasks.add(t)
        t.add_done_callback(self._scoped_tasks.discard)

    async def _hold(self, spec) -> None:
        """A plain state's timeout."""
        timeout = self.num(spec.timeout)
        if not timeout:
            return
        await asyncio.sleep(timeout)
        self._enter(spec.then or self.show.initial, "timeout")

    async def _after_crash(self, name: str) -> None:
        self._crashes = getattr(self, "_crashes", 0) + 1
        await asyncio.sleep(min(2 * self._crashes, 10))
        self._enter(self.show.initial, "after crash")

    async def _run_async(self, spec) -> None:
        name = spec.name
        try:
            nxt, reason = await self._run_state(spec)
            self._crashes = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = f"state {name} crashed: {_describe(e, self.piece.path)}"
            self.log(self.error, "error")
            await self._after_crash(name)
            return
        self._enter(nxt, reason)

    async def _run_state(self, spec) -> tuple[str, str]:
        async def body() -> str:
            nxt = await _call(spec.fn, self.ctx)
            if nxt is None:
                await asyncio.Future()  # stay until a timeout or someone else moves the show
            return nxt if isinstance(nxt, str) else nxt.__name__

        timeout = self.num(spec.timeout)
        try:
            if not timeout:
                return await body(), ""
            async with asyncio.timeout(timeout):
                return await body(), ""
        except TimeoutError:
            return spec.then or self.show.initial, "timeout"
        except Goto as g:
            return g.name, "goto"

    def load_yaml(self, name: str) -> Any:
        path = self.piece.path / name
        mtime = path.stat().st_mtime if path.exists() else 0.0
        cached = self._yaml_cache.get(name)
        if not cached or cached[0] != mtime:
            cached = (mtime, self.piece.load_yaml(name))
            self._yaml_cache[name] = cached
        return cached[1]

    def reset(self) -> None:
        self.vars.clear()
        self.keep.clear()
        self.goto(self.show.initial, "reset", before=self._run_reset_fns)

    async def _run_reset_fns(self) -> None:
        for fn in self.show.on_reset_fns:
            try:
                await _call(fn, self.ctx)
            except Exception as e:
                self.log(f"on_reset failed: {_describe(e, self.piece.path)}", "error")

    async def _every(self, every) -> None:
        while True:
            await asyncio.sleep(self.num(every.seconds) or 1)
            if every.states and self.state not in every.states:
                continue
            try:
                await _call(every.fn, self.ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"every {every.fn.__name__} failed: {_describe(e, self.piece.path)}", "error")

    # -- events ---------------------------------------------------------------
    def event(self, ev: Event) -> None:
        if ev.source != "show" and ev.kind != "value":
            self.last_input = ev.t
        for waiter in list(self.waiters):
            patterns, where, fut = waiter
            if fut.done() or not any(fnmatch.fnmatchcase(ev.name, p) for p in patterns):
                continue
            try:
                if where and not where(ev):
                    continue
            except Exception as e:
                self.log(f"wait(where=...) failed: {e}", "error")
                continue
            fut.set_result(ev)
        if self.show:
            for h in self.show.handlers:
                if fnmatch.fnmatchcase(ev.name, h.pattern):
                    self.dispatch(h.fn, (self.ctx, ev), f"on {h.pattern}")
        handlers, entries = list(self.scoped_handlers), self.entries
        for pattern, fn in handlers:
            if self.entries != entries:
                break  # the state is over: the rest of its handlers do not hear this
            if fnmatch.fnmatchcase(ev.name, pattern):
                self.dispatch(fn, (ev,), f"{self.state}: on {pattern}")

    def dispatch(self, fn: Callable, args: tuple, label: str) -> None:
        """Run a handler now if it is a plain function, so its effects are in order;
        as a task if it is async."""
        if inspect.iscoroutinefunction(fn):
            self._spawn(fn, args, label)
            return
        try:
            r = fn(*args)
            if asyncio.iscoroutine(r):
                self._spawn(lambda: r, (), label)
        except Goto as g:
            if asyncio.current_task() is self._runner:
                raise  # raised inside the state itself: let the state end on it
            self.goto(g.name, label)
        except Exception as e:
            self.log(f"{label} failed: {_describe(e, self.piece.path)}", "error")

    async def guard(self, fn: Callable, args: tuple, label: str) -> None:
        try:
            await _call(fn, *args)
        except asyncio.CancelledError:
            raise
        except Goto as g:
            self.goto(g.name, label)
        except Exception as e:
            self.log(f"{label} failed: {_describe(e, self.piece.path)}", "error")

    def pick(self, patterns: tuple[str, ...]) -> str | None:
        now = time.time()
        if now - self._media_cache[0] > 5:
            root = self.piece.media
            files = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                           if p.is_file() and not p.name.startswith(".")) if root.is_dir() else []
            self._media_cache = (now, files)
        files = self._media_cache[1]
        found = [f for f in files
                 if any(fnmatch.fnmatchcase(f, p) or fnmatch.fnmatchcase(f.rsplit(".", 1)[0], p)
                        for p in patterns)]
        if not found:
            return None
        last = self._picked.get(patterns)
        choices = [f for f in found if f != last] or found
        choice = random.choice(choices)
        self._picked[patterns] = choice
        return choice

    def _spawn(self, fn: Callable, args: tuple, label: str) -> None:
        async def guarded():
            try:
                await _call(fn, *args)
            except asyncio.CancelledError:
                raise
            except Goto as g:
                self.goto(g.name, label)
            except Exception as e:
                self.log(f"{label} failed: {_describe(e, self.piece.path)}", "error")

        t = asyncio.create_task(guarded())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _on_input(self, topic: str, payload: Any) -> None:
        name, _, input_ = topics.split_node_topic(topic)
        if not isinstance(payload, dict):
            payload = {"event": "value", "value": payload}
        kind = payload.get("event", "value")
        value = payload.get("value")
        if value is not None:
            self.values[f"{name}.{input_}"] = value
        self.event(Event(f"{name}.{input_}.{kind}", value, source=payload.get("source", "node")))

    def _on_status(self, topic: str, payload: Any) -> None:
        name = topics.split_node_topic(topic)[0]
        if payload is None:
            self.nodes.pop(name, None)
            return
        was = self.nodes.get(name, {}).get("online")
        self.nodes[name] = payload
        now = payload.get("online")
        if now and not was:
            if name in self.devices:
                self.log(f"{name} online")
            elif name == payload.get("id"):
                self.log(f"unassigned node {name} online: add `id: {name}` to a node in devices.yaml",
                         "warn")
            self.resend(name)
        elif was and not now:
            self.log(f"{name} offline", "warn")

    # -- outputs --------------------------------------------------------------
    def send(self, node: str, output: str, spec: dict, persist: bool) -> None:
        if self.devices and node not in self.devices and node not in self._warned:
            self._warned.add(node)
            self.log(f"sending {output} to {node!r}, which is not in devices.yaml", "warn")
        if persist:
            self.outputs[(node, output)] = spec
        self._send(node, output, spec)

    def _send(self, node: str, output: str, spec: dict) -> None:
        if self.blackout and output == "leds":
            return
        try:
            payload = {k: (v(self.params) if callable(v) else v) for k, v in spec.items()}
        except Exception as e:
            self.log(f"{node}.{output}: {_describe(e, self.piece.path)}", "error")
            return
        self._pub(topics.node_out(node, output), payload)

    def resend(self, node: str) -> None:
        for (n, output), spec in self.outputs.items():
            if n == node:
                self._send(n, output, spec)

    def resend_all(self, only_bound: bool = False) -> None:
        for (n, output), spec in self.outputs.items():
            if not only_bound or any(callable(v) for v in spec.values()):
                self._send(n, output, spec)

    def set_blackout(self, on: bool) -> None:
        self.blackout = on
        if on:
            for node in {n for n, _ in self.outputs} | set(self.devices):
                self._pub(topics.node_out(node, "leds"), {"effect": "off", "fade": 0.3})
        else:
            self.resend_all()
        self.log("blackout on" if on else "blackout off")
        self._publish_state()

    # -- commands (web UI, CLI, Claude) ---------------------------------------
    async def _on_cmd(self, topic: str, payload: Any) -> None:
        verb = topic.rsplit("/", 1)[1]
        p = payload if isinstance(payload, dict) else {}
        if not self.show and verb not in ("param", "blackout"):
            self.log(f"cmd {verb} ignored: no show loaded ({self.error})", "error")
            return
        if verb == "goto":
            self.goto(p.get("state", ""), "operator")
        elif verb == "reset":
            self.log("reset by operator")
            self.reset()
        elif verb == "param":
            try:
                v = self.params.set(p["name"], p["value"])
            except (KeyError, ValueError, TypeError) as e:
                self.log(f"param: {e}", "error")
                return
            self._pub(topics.SHOW_PARAMS, self.params.snapshot(), retain=True)
            self.resend_all(only_bound=True)
            self.event(Event(f"param.{p['name']}.set", v, source="operator"))
        elif verb == "param_reset":
            self.params.reset(p.get("name"))
            self._pub(topics.SHOW_PARAMS, self.params.snapshot(), retain=True)
            self.resend_all(only_bound=True)
        elif verb == "action":
            action = self.show.actions.get(p.get("name", ""))
            if not action:
                self.log(f"no action {p.get('name')!r}", "error")
                return
            self.log(f"action: {action.label}")
            self._spawn(action.fn, (self.ctx,), f"action {action.label}")
        elif verb == "blackout":
            self.set_blackout(bool(p.get("on", not self.blackout)))
        elif verb == "event":
            self.event(Event(p.get("name", ""), p.get("value"), source="operator"))
        elif verb == "identify":
            self._pub(topics.node_out(p.get("node", ""), "identify"), {"seconds": 3})

    # -- helpers --------------------------------------------------------------
    def num(self, v: float | str | None) -> float | None:
        """A number, or the name of a param holding one."""
        if v is None or isinstance(v, (int, float)):
            return v
        return float(self.params.get(v))

    def log(self, msg: str, level: str = "info") -> None:
        getattr(log, {"warn": "warning"}.get(level, level), log.info)(msg)
        self._pub(topics.SHOW_LOG, {"t": time.time(), "level": level, "msg": msg})

    def _publish_state(self) -> None:
        show = self.show
        self._pub(topics.SHOW_STATE, {
            "piece": self.piece.name,
            "state": self.state,
            "since": self.since,
            "initial": show.initial if show else None,
            "idle_reset": self.num(show.idle_reset) if show else None,
            "states": [{"name": s.name, "timeout": self.num(s.timeout), "then": s.then, "doc": s.doc}
                       for s in show.states.values()] if show else [],
            "actions": [{"label": a.label, "doc": a.doc} for a in show.actions.values()] if show else [],
            "devices": self.devices,
            "error": self.error,
            "blackout": self.blackout,
        }, retain=True)

    def _pub(self, topic: str, payload: Any, retain: bool = False) -> None:
        t = asyncio.get_running_loop().create_task(self.bus.publish(topic, payload, retain))
        self._pubs.add(t)
        t.add_done_callback(self._pubs.discard)


async def _call(fn: Callable, *args: Any) -> Any:
    """Call a show function, async or not."""
    r = fn(*args)
    if asyncio.iscoroutine(r):
        r = await r
    return r


def _check_param_refs(show: Show, specs: dict) -> None:
    """Timeouts and intervals may name a param; catch a typo at load, not mid-show."""
    refs = [("idle_reset", show.idle_reset)]
    refs += [(f"state {s.name} timeout", s.timeout) for s in show.states.values()]
    refs += [(f"every {e.fn.__name__}", e.seconds) for e in show.timers]
    for what, ref in refs:
        if isinstance(ref, str) and ref not in specs:
            raise PieceError(f"{what} = {ref!r}, but there is no param {ref!r} in params.yaml")


def _describe(e: BaseException, piece_dir: Path) -> str:
    """One line: the error, and where in the piece's own files it happened."""
    if isinstance(e, PieceError):
        return str(e)
    where = ""
    if isinstance(e, SyntaxError) and e.filename:
        return f"SyntaxError: {e.msg} ({Path(e.filename).name}:{e.lineno})"
    else:
        for frame in reversed(traceback.extract_tb(e.__traceback__)):
            if Path(frame.filename).parent == piece_dir:
                where = f" ({Path(frame.filename).name}:{frame.lineno}: {frame.line})"
                break
    return f"{type(e).__name__}: {e}{where}"

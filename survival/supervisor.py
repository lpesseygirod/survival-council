"""Protected supervisor: runs the watchdog and owns every AI process.

The brain (phase 2) and the AI's always-on scripts (phase 3) are started here,
each in its own process group, so that:
  - pause  = SIGSTOP to every group (frozen, not killed; the clock keeps running)
  - resume = SIGCONT
  - death  = SIGTERM, then SIGKILL, to every group, and nothing is ever restarted
On the server the same AI processes also sit in their own systemd slice (phase 6),
whose cgroup freezer/stop catches anything that tried to escape its group.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .clock import Clock, iso
from .config import Config, Paths
from .watchdog import Watchdog, state_store

if False:  # typing only
    from .waker import Waker

log = logging.getLogger("council.supervisor")

EXIT_DEAD = 3  # systemd: RestartPreventExitStatus=3


@dataclass
class Managed:
    name: str
    proc: subprocess.Popen


class AIProcesses:
    """Registry of the AI's processes (brain + scripts)."""

    def __init__(self, signal_all: Callable[[str], None] | None = None) -> None:
        self._procs: dict[str, Managed] = {}
        self._lock = threading.Lock()
        self.frozen = False
        self.stopped = False
        # server: also signal every process of the sandbox user (STOP/CONT/KILL)
        self._signal_all = signal_all or (lambda sig: None)

    def spawn(self, name: str, argv: list[str], **kw) -> subprocess.Popen:
        with self._lock:
            if self.stopped:
                raise RuntimeError("AI processes are stopped for good")
            p = subprocess.Popen(argv, start_new_session=True, **kw)
            self._procs[name] = Managed(name, p)
            if self.frozen:
                self._signal(p, signal.SIGSTOP)
            return p

    def names(self) -> list[str]:
        with self._lock:
            return [n for n, m in self._procs.items() if m.proc.poll() is None]

    def is_alive(self, name: str) -> bool:
        with self._lock:
            m = self._procs.get(name)
            return bool(m and m.proc.poll() is None)

    def returncode(self, name: str) -> int | None:
        with self._lock:
            m = self._procs.get(name)
            return m.proc.poll() if m else None

    def stop(self, name: str, grace: float = 5.0) -> None:
        with self._lock:
            m = self._procs.get(name)
        if not m or m.proc.poll() is not None:
            return
        self._signal(m.proc, signal.SIGCONT)
        self._signal(m.proc, signal.SIGTERM)
        try:
            m.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal(m.proc, signal.SIGKILL)
            m.proc.wait(timeout=5)

    @staticmethod
    def _signal(p: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            pass

    def pause(self) -> None:
        with self._lock:
            self.frozen = True
            for m in self._procs.values():
                self._signal(m.proc, signal.SIGSTOP)
        self._signal_all("STOP")

    def resume(self) -> None:
        with self._lock:
            self.frozen = False
            for m in self._procs.values():
                self._signal(m.proc, signal.SIGCONT)
        self._signal_all("CONT")

    def stop_all(self, grace: float = 5.0) -> None:
        with self._lock:
            self.stopped = True
            procs = list(self._procs.values())
        for m in procs:
            self._signal(m.proc, signal.SIGCONT)  # a frozen process can't handle SIGTERM
            self._signal(m.proc, signal.SIGTERM)
        deadline = time.monotonic() + grace
        for m in procs:
            try:
                m.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            self._signal(m.proc, signal.SIGKILL)
        self._signal_all("KILL")
        log.info("stopped AI processes: %s", [m.name for m in procs])


def is_finally_dead(paths: Paths) -> bool:
    """DEAD and nothing left to sell: refuse to start, ever."""
    if not paths.dead_marker.exists():
        return False
    st = state_store(paths, "").read()
    return bool(st.get("death", {}).get("liquidation", {}).get("done"))


class Supervisor:
    def __init__(self, cfg: Config, paths: Paths, mode: str,
                 make_watchdog: Callable[[AIProcesses], Watchdog], clock: Clock | None = None,
                 make_waker: Callable[[AIProcesses], "Waker"] | None = None,
                 ai: AIProcesses | None = None, gateway=None,
                 make_scripts: Callable[[AIProcesses], Any] | None = None):
        self.cfg, self.paths, self.mode = cfg, paths, mode
        self.clock = clock or Clock()
        self.ai = ai or AIProcesses()
        self.gateway = gateway
        self.scripts = make_scripts(self.ai) if make_scripts else None
        self.watchdog = make_watchdog(self.ai)
        self.waker = make_waker(self.ai) if make_waker else None
        self.state = state_store(paths, mode)
        self._stop = threading.Event()

    def run(self) -> int:
        if is_finally_dead(self.paths):
            log.error("this %s run is dead; it will never start again", self.mode)
            return EXIT_DEAD
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        if not (self.paths.root.parent / "current_mode").exists():
            (self.paths.root.parent / "current_mode").write_text(self.mode)

        wd = threading.Thread(target=self.watchdog.run_forever,
                              kwargs={"should_stop": self._stop.is_set},
                              name="watchdog", daemon=True)
        wd.start()
        self.state.set(supervisor={"pid": os.getpid(), "started_at": iso(self.clock.now())})
        if self.gateway and not self.paths.dead_marker.exists():
            self.gateway.start()
        if self.scripts:
            self.scripts.on_boot()

        mode_file = self.paths.root.parent / "current_mode"
        while not self._stop.is_set():
            if mode_file.exists() and mode_file.read_text().strip() != self.mode:
                log.info("mode switched to %s by Lea; restarting", mode_file.read_text().strip())
                break  # systemd starts `./council run` again, which follows current_mode
            self.apply_controls()
            if not self.ai.stopped and not self.paths.paused_flag.exists():
                for part in (self.scripts.reconcile if self.scripts else None,
                             self.waker.check if self.waker else None):
                    if part is None:
                        continue
                    try:
                        part()
                    except Exception as e:
                        log.error("%s: %s", getattr(part, "__qualname__", part), e)
            if not wd.is_alive():
                break
            self._stop.wait(1.0)

        self._stop.set()
        if self.gateway:
            self.gateway.stop()
        self.ai.stop_all()
        wd.join(timeout=10)
        return EXIT_DEAD if self.paths.dead_marker.exists() else 0

    def apply_controls(self) -> None:
        if self.paths.dead_marker.exists():
            if not self.ai.stopped:
                self.ai.stop_all()
            return
        paused = self.paths.paused_flag.exists()
        if paused and not self.ai.frozen:
            self.ai.pause()
            log.info("paused by Lea")
        elif not paused and self.ai.frozen:
            self.ai.resume()
            log.info("resumed by Lea")

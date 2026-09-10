"""Worker Supervisor — owned by the web server.

Launches the worker as a real OS subprocess so the operator can SIGKILL it ("Crash
worker") and relaunch it ("Restart worker") on demand. A real process death — not a thread
or an exception — is what makes Temporal observe the worker stop polling / heartbeating,
exactly as in production. The supervisor never makes the worker self-restart.

The **Agent Only** variant uses no Temporal and therefore no worker: selecting it stops the
worker and start() becomes a no-op until a Temporal variant is selected again.
"""

from __future__ import annotations

import signal
import subprocess
import sys

import app.config as config

# The three variants backed by a Temporal worker (Agent Only is handled in-process).
WORKER_VARIANTS = config.WORKER_VARIANTS
ALL_VARIANTS = config.ALL_VARIANTS


class WorkerSupervisor:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._variant: str = config.AGENT

    @property
    def variant(self) -> str:
        return self._variant

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, variant: str | None = None) -> None:
        if variant:
            self._variant = variant
        # Agent Only has no worker — nothing to launch.
        if self._variant == config.AGENT_ONLY:
            return
        if self.is_running():
            return
        # Inherit env (carries OPENAI_API_KEY etc.); stdout/stderr inherit so worker logs
        # land in the web server's console.
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker.worker_main", "--variant", self._variant]
        )

    def stop(self) -> None:
        """SIGKILL the worker. It stays down until start() is called again."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGKILL)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        self._proc = None

    def restart(self, variant: str | None = None) -> None:
        self.stop()
        self.start(variant)

    def set_variant(self, variant: str) -> None:
        """Switch variant — relaunches the worker (or stops it for Agent Only)."""
        if variant not in ALL_VARIANTS:
            raise ValueError(f"unknown variant {variant!r}")
        self._variant = variant
        if variant == config.AGENT_ONLY:
            self.stop()
        else:
            self.restart(variant)

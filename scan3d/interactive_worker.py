"""Supervised nnInteractive worker process, owned by the GPU lane.

The warm engine, `interactive.InteractiveRuntime`, lives in one long-lived
child process that the first prediction starts. A GPU driver fault or hung kernel
then fails one job instead of the web server, and the next request starts a
fresh child. Process exit is also the only way to return every byte of VRAM:
emptying caches leaves the GPU context and allocator pools resident. The API
process never imports torch; the child imports it inside the engine.

The child is a plain subprocess in its own session rather than a
multiprocessing child: it never re-imports the server's __main__, it runs this
exact release of scan3d, and a forced stop signals its whole process group,
including native helpers. Messages are newline-delimited JSON on an inherited
socket pair, so nothing is unpickled, provenance stays JSON as SQLite stores
it, and stdout/stderr stay free for native logs in the service journal:

    parent -> child  {"op": "predict", "id": N, "args": {...}}
                     {"op": "cancel", "id": N}
                     {"op": "shutdown"}
    child -> parent  {"event": "ready", "pid": P, "protocol": 1}
                     {"event": "progress", "id": N, "message": "..."}
                     {"event": "result", "id": N, "provenance": {...}}
                     {"event": "error", "id": N, "kind": "cancelled|invalid|failed", "message": "..."}
"""

from __future__ import annotations

import faulthandler
from importlib import import_module
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import time

from . import inference_paths
from .config import ROOT
from .segmentation import SegmentationCancelled

log = logging.getLogger("scan3d.interactive_worker")

PROTOCOL = 1
ENGINE = "scan3d.interactive:InteractiveRuntime"
# The engine checks cancellation between model steps, each lasting seconds.
CANCEL_GRACE_SECONDS = 10.0
# A hang shows as silence, not elapsed time. A cold model load is the longest
# step without progress and takes far less than this on a GPU.
STALL_SECONDS = 600.0
STOP_SECONDS = 5.0
_ERRORS = {"cancelled": SegmentationCancelled, "invalid": ValueError, "failed": RuntimeError}


class _Channel:
    """Newline-delimited JSON over one stream socket; JSON never has a raw newline."""

    def __init__(self, sock):
        self.socket, self._buffer, self._sending = sock, bytearray(), threading.Lock()

    def send(self, message, timeout=None):
        data = json.dumps(message, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        with self._sending:
            self.socket.settimeout(timeout)
            self.socket.sendall(data)

    def receive(self, timeout=None):
        """Return the next message, or None after `timeout`; raise EOFError once closed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while (end := self._buffer.find(b"\n")) < 0:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            self.socket.settimeout(remaining)
            try:
                chunk = self.socket.recv(1 << 20)
            except TimeoutError:
                return None
            if not chunk:
                raise EOFError("The nnInteractive worker channel closed")
            self._buffer += chunk
        line = bytes(self._buffer[:end])
        del self._buffer[: end + 1]
        return json.loads(line)

    def close(self):
        self.socket.close()


class _Child:
    """One worker process, its channel, its inference path and whether it has finished starting."""

    def __init__(self, engine, inference_path=None):
        self.inference_path = inference_path
        parent, child = socket.socketpair()
        try:
            env = inference_path.environment() if inference_path else dict(os.environ)
            # Import exactly this release of scan3d: -P ignores the working
            # directory, and this checkout precedes any inherited path.
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH"))))
            self.process = subprocess.Popen(
                [inference_path.python if inference_path else sys.executable, "-P", "-m", "scan3d.interactive_worker", str(child.fileno()), engine],
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.channel = _Channel(parent)
        self.ready = False

    def stop(self, graceful):
        """Stop the process group; return whether the process has exited."""
        process = self.process
        try:
            if graceful and process.poll() is None:
                try:
                    # The engine releases its session; exit returns all VRAM.
                    self.channel.send({"op": "shutdown"}, timeout=STOP_SECONDS)
                    process.wait(timeout=2 * STOP_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for number in (signal.SIGTERM, signal.SIGKILL):
                if process.poll() is not None:
                    break
                try:
                    os.killpg(process.pid, number)
                    process.wait(timeout=STOP_SECONDS)
                except ProcessLookupError:
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self.channel.close()
        return process.poll() is not None

    def ended(self):
        """Describe how a child that closed its channel ended."""
        try:
            code = self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self.stop(graceful=False)
            code = self.process.poll()
        self.channel.close()
        if code is None:
            return "stopped responding"
        if code < 0:
            try:
                return f"was terminated by {signal.Signals(-code).name}"
            except ValueError:
                return f"was terminated by signal {-code}"
        return f"exited with status {code}"


class InteractiveWorker:
    """InteractiveRuntime's predict/release interface, served by a child process.

    The GPU lane owns this object and sends one request at a time. The child
    keeps one engine between requests, so warm reuse, the unchanged-prefix
    fast path, replay on undo, checkpoint verification and provenance are
    exactly InteractiveRuntime's. A cancelled or invalid request keeps the
    child; a failure, crash or hang replaces it at the next request.

    A child also belongs to one inference path, because a process keeps the GPU
    runtime it started with. A request for any other path, the default among
    them, stops the child and verifies its exit before starting the next, so
    one inference process exists across all GPUs. The device reaches the child
    through its spawn environment, so the protocol and the engine never see a
    path. The comparison stays here, where no fake runtime is ever compared.
    """

    def __init__(self, engine=ENGINE, *, cancel_grace=CANCEL_GRACE_SECONDS, stall_seconds=None):
        self.engine, self.cancel_grace = engine, cancel_grace
        self.stall_seconds = float(os.environ.get("SCAN3D_NNINTERACTIVE_STALL_SECONDS", STALL_SECONDS)) if stall_seconds is None else stall_seconds
        self._child = None
        self._requests = itertools.count(1)
        self._lock = threading.Lock()

    @property
    def pid(self):
        """The running child's process ID, for diagnostics; None when stopped."""
        child = self._child
        return child.process.pid if child is not None and child.process.poll() is None else None

    def predict(self, ct_path, mask_path, prompts, output_path, progress, cancelled, session_key, *, batches=None, options=None, inference_path=None):
        args = {
            "ct_path": str(ct_path),
            "mask_path": str(mask_path),
            "prompts": prompts,
            "output_path": str(output_path),
            "session_key": session_key,
            "batches": batches,
            "options": options,
        }
        with self._lock:
            if cancelled():
                raise SegmentationCancelled("nnInteractive correction was cancelled")
            if self._child is not None and self._child.process.poll() is not None:
                self._stop()  # It ended while idle; this request starts afresh.
            if self._child is not None and self._child.inference_path != inference_path:
                # Equality is full identity, so a proof that changes a field is another path, and
                # the default is None, so leaving an added path takes this branch too.
                label = inference_path.label if inference_path else inference_paths.binding(inference_paths.DEFAULT)["label"]
                progress(f"Switching the AI worker to {label}")
                self._shutdown()
            if self._child is None:
                self._child = _Child(self.engine, inference_path)
            return self._exchange(next(self._requests), args, progress, cancelled)

    def _exchange(self, request, args, progress, cancelled):
        """Relay progress until the child answers; cancel cooperatively, then by force."""
        child, reason, deadline, sent, settled = self._child, None, None, False, False
        heard = time.monotonic()
        try:
            while True:
                if child.ready and not sent:
                    try:
                        child.channel.send({"op": "predict", "id": request, "args": args}, timeout=STOP_SECONDS)
                    except OSError:
                        pass  # A lost child reports itself below.
                    sent = True
                if reason is None:
                    try:
                        if cancelled():
                            raise SegmentationCancelled("nnInteractive correction was cancelled")
                    except BaseException as exc:
                        reason = exc
                if reason is not None and not sent:
                    settled = True  # Nothing was requested; a starting child stays usable.
                    raise reason
                if reason is not None and deadline is None:
                    deadline = time.monotonic() + self.cancel_grace
                    try:
                        child.channel.send({"op": "cancel", "id": request}, timeout=STOP_SECONDS)
                    except OSError:
                        pass
                if deadline is not None and time.monotonic() > deadline:
                    settled = True
                    self._stop()
                    if isinstance(reason, InterruptedError):
                        raise SegmentationCancelled(
                            f"nnInteractive did not stop within {self.cancel_grace:g} s of cancellation; its worker process was stopped"
                        ) from reason
                    raise reason
                if time.monotonic() - heard > self.stall_seconds:
                    settled = True
                    self._stop()
                    raise RuntimeError(f"nnInteractive reported no progress for {self.stall_seconds:g} s; its worker process was stopped")
                try:
                    message = child.channel.receive(0.2)
                except (EOFError, OSError, ValueError):
                    settled, self._child = True, None
                    ending = child.ended()
                    if reason is not None:
                        raise reason from None
                    if not child.ready:
                        raise RuntimeError(f"The nnInteractive worker process could not start; it {ending}. Inspect the service log") from None
                    raise RuntimeError(f"The nnInteractive worker process {ending} during this correction; the next correction starts a fresh worker") from None
                if message is None:
                    continue
                heard, event = time.monotonic(), message.get("event")
                if event == "ready":
                    if message.get("protocol") != PROTOCOL:
                        # The checkout changed under a running server; restart it.
                        settled = True
                        self._stop()
                        raise RuntimeError("The nnInteractive worker process runs different scan3d code; restart the service")
                    child.ready = True
                elif message.get("id") != request:
                    continue
                elif event == "progress":
                    if reason is None:
                        try:
                            progress(message.get("message", ""))
                        except BaseException as exc:  # The job was cancelled or superseded.
                            reason = exc
                elif event in ("result", "error"):
                    settled = True
                    if reason is not None:
                        raise reason
                    if event == "result":
                        return message["provenance"]
                    if message.get("kind") == "failed":
                        # A GPU fault can poison the driver context; start the next one clean.
                        self._stop()
                    raise _ERRORS.get(message.get("kind"), RuntimeError)(message.get("message") or "nnInteractive correction failed")
        finally:
            if not settled:
                self._stop()

    def _stop(self):
        child, self._child = self._child, None
        if child is not None and not child.stop(graceful=False):
            log.error("nnInteractive worker process %s did not exit; its GPU memory may remain in use", child.process.pid)

    def _shutdown(self):
        """Stop the child gracefully and verify its exit.

        This takes no lock, because predict replaces a child while holding the one release takes.
        """
        child, self._child = self._child, None
        if child is not None and not child.stop(graceful=True):
            raise RuntimeError("The nnInteractive worker process did not exit; its GPU memory may still be in use")

    def release(self):
        """Stop the child before TotalSegmentator runs and at shutdown.

        The engine releases its session, then process exit returns all VRAM,
        including the GPU context. The next prediction starts a fresh child.
        """
        with self._lock:
            self._shutdown()


def _serve(channel, engine):
    """Answer one request at a time; a listener thread receives cancellations."""
    cancelled, requests = [None], queue.SimpleQueue()

    def listen():
        try:
            while True:
                message = channel.receive()
                if message.get("op") == "cancel":
                    cancelled[0] = message.get("id")
                else:
                    requests.put(message)
        except (EOFError, OSError, ValueError):
            pass
        # The API process is gone or unreadable, so nobody can use this GPU
        # state. Exit now, even while a kernel still runs on the main thread.
        os._exit(1)

    threading.Thread(target=listen, name="scan3d-interactive-channel", daemon=True).start()
    channel.send({"event": "ready", "pid": os.getpid(), "protocol": PROTOCOL})
    while (message := requests.get()).get("op") != "shutdown":
        if message.get("op") != "predict":
            continue
        request, args = message.get("id"), message.get("args") or {}
        try:
            provenance = engine.predict(
                Path(args["ct_path"]),
                Path(args["mask_path"]),
                args["prompts"],
                Path(args["output_path"]),
                lambda text: channel.send({"event": "progress", "id": request, "message": str(text)}),
                lambda: cancelled[0] == request,
                args["session_key"],
                batches=args.get("batches"),
                options=args.get("options"),
            )
            channel.send({"event": "result", "id": request, "provenance": provenance})
        except Exception as exc:
            kind = "cancelled" if isinstance(exc, InterruptedError) else "invalid" if isinstance(exc, ValueError) else "failed"
            if kind == "failed":
                log.exception("nnInteractive request %s failed", request)
            channel.send({"event": "error", "id": request, "kind": kind, "message": str(exc) or type(exc).__name__})
    engine.release()


def main(argv=None):
    """Child entry point: python -P -m scan3d.interactive_worker FD MODULE:FACTORY."""
    descriptor, engine = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="scan3d.interactive_worker[%(process)d] %(levelname)s %(message)s")
    # A native fault prints the Python stack to the service journal.
    faulthandler.enable()
    channel = _Channel(socket.socket(fileno=int(descriptor)))
    module, _, factory = engine.partition(":")
    _serve(channel, getattr(import_module(module), factory)())


if __name__ == "__main__":
    main()

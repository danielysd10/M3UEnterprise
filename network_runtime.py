"""Cancelable leaf processes for Enterprise DNS and complete HTTP validation.

Deadlines use time.monotonic(), including process startup and response parsing.
These workers never start descendants: cancellation kills and reaps the direct
child before returning. Live-response HTTP APIs in safe_http remain unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

POLL_SECONDS = 0.1
# Windows venv python.exe is a redirector that may create a second process.
# Workers use only stdlib and sibling modules (the application has no runtime
# dependencies); launch the real interpreter so Popen.pid is the owned worker.
WORKER_PYTHON = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable


@contextmanager
def cancel_on_sigint(cancel_event):
    """Let a foreground CLI drain its workers and then report interruption."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, lambda signum, frame: cancel_event.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class NetworkCancelled(RuntimeError):
    """The caller requested cancellation; the owned child has been reaped."""


class NetworkDeadlineExceeded(TimeoutError):
    """The total operation deadline expired; the owned child has been reaped."""


class NetworkWorkerError(RuntimeError):
    """The isolated operation failed without returning a valid result."""


class _WindowsJob:
    """Non-inheritable KILL_ON_JOB_CLOSE handle owned by the controller.

    Windows closes the handle even when the controller is terminated abruptly.
    This protects children after assign(); Popen and assignment are not atomic.
    https://learn.microsoft.com/windows/win32/procthread/job-objects
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel, self._ctypes = kernel, ctypes
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        # Use Popen's exact Windows handle, not a PID lookup subject to reuse.
        if not self._kernel.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self):
        if self._handle:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                raise self._ctypes.WinError(self._ctypes.get_last_error())


def _check_boundary(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise NetworkCancelled("Validación de red cancelada.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NetworkDeadlineExceeded("Timeout: venció el plazo total de validación de red.")
    return remaining


def _run_owned_process(command, *, input_text=None, deadline, cancel_event=None):
    """Run one leaf child, never returning while that child is still running.

    Callers must use a leaf executable, never a shell or Python venv redirector.
    Input uses an
    anonymous temporary file instead of a pipe: Windows communicate() can block
    while writing a full stdin pipe before honoring its timeout. Output pipes
    are drained with communicate(), including after kill, so its reader threads
    are joined. OS process creation/termination latency is not a real-time SLA.
    """
    deadline = float(deadline)
    if not math.isfinite(deadline):
        raise ValueError("deadline debe ser un instante monotónico finito.")
    _check_boundary(deadline, cancel_event)
    with tempfile.TemporaryFile() as input_file:
        if input_text is not None:
            input_file.write(str(input_text).encode("utf-8"))
        input_file.seek(0)
        _check_boundary(deadline, cancel_event)
        job = _WindowsJob() if os.name == "nt" else None
        process = None
        try:
            process = subprocess.Popen(
                command, stdin=input_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if job is not None:
                job.assign(process)
            while True:
                remaining = _check_boundary(deadline, cancel_event)
                try:
                    stdout, stderr = process.communicate(timeout=min(POLL_SECONDS, remaining))
                    _check_boundary(deadline, cancel_event)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # On Windows, communicate uses pipe-reader threads. A final drain
            # joins them even if cancellation happened between poll intervals.
            try:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
            finally:
                if job is not None:
                    job.close()


def run_operation(operation, payload, *, deadline, cancel_event=None):
    """Run DNS, HTTP validation or XMLTV staging in a supervised leaf process.

    XMLTV writes to a private staging location and returns aggregate metadata.
    Requests use stdin so URLs and paths are absent from process arguments.
    """
    if operation not in {"dns", "http", "xmltv", "validator_http"}:
        raise ValueError("Operación de red no reconocida.")
    request = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=True)
    process = _run_owned_process(
        [WORKER_PYTHON, "-I", "-B", str(Path(__file__).resolve())],
        input_text=request, deadline=deadline, cancel_event=cancel_event,
    )
    if process.returncode:
        # stderr may contain private request data. Do not promote it to reports.
        raise NetworkWorkerError(f"El worker de red terminó con código {process.returncode}.")
    try:
        response = json.loads(process.stdout)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("Invalid worker envelope")
        if not response["ok"]:
            if operation == "dns" and response.get("error_type") == "gaierror":
                raise socket.gaierror(response.get("error", "No se pudo resolver DNS."))
            raise NetworkWorkerError("La operación de red aislada no pudo completarse.")
        result = response["result"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise NetworkWorkerError("Respuesta inválida del worker de red.") from exc
    _check_boundary(float(deadline), cancel_event)
    return result


def _worker_main():
    # -I excludes the caller's cwd/PYTHONPATH. Import only the sibling modules
    # installed alongside this file, after reading the request from stdin.
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    operation = request["operation"]
    payload = request["payload"]
    try:
        if operation == "dns":
            result = socket.getaddrinfo(payload["host"], payload.get("port", 0), type=socket.SOCK_STREAM)
        elif operation == "http":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from M3U_PRO_V9_ENGINE import validate_url_http
            result = validate_url_http(payload["entry"], payload["timeout"])
        elif operation == "validator_http":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from M3U_PRO_VALIDATOR import _probe_validator_http
            result = _probe_validator_http(payload["url"], payload["user_agent"], payload["timeout"])
        elif operation == "xmltv":
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from channel_metadata import _fetch_xmltv
            result = _fetch_xmltv(payload["url"], Path(payload["destination"]),
                                  payload["max_bytes"], payload["max_uncompressed_bytes"])
        else:
            raise ValueError("Unknown operation")
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error_type": type(exc).__name__,
                    "error": str(exc) if isinstance(exc, socket.gaierror) else "Worker failed"}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=True).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    _worker_main()

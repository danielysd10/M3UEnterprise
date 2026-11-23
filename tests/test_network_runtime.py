"""Real owned processes and loopback HTTP; no remote streams or DNS changes."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
from socketserver import ThreadingTCPServer
import subprocess
import sys
import threading
import time

import pytest

import network_runtime as runtime


@pytest.fixture
def owned_processes(monkeypatch):
    processes = []
    original = subprocess.Popen

    def observe(*args, **kwargs):
        child = original(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", observe)
    yield processes
    try:
        assert all(child.poll() is not None for child in processes), "Worker survived operation return"
        assert all(child.stdout.closed and child.stderr.closed for child in processes), "Output pipe leaked"
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
                child.communicate()


def test_dns_numeric_address_and_no_private_request_in_argv(owned_processes):
    result = runtime.run_operation("dns", {"host": "127.0.0.1", "port": 8123}, deadline=time.monotonic() + 10)
    assert result and all(row[4][0] == "127.0.0.1" for row in result)
    assert all(row[4][1] == 8123 for row in result)
    assert "127.0.0.1" not in " ".join(owned_processes[0].args)


@pytest.mark.parametrize("cancelled", [False, True])
def test_slow_resolver_dependency_is_killed_and_reaped(tmp_path, owned_processes, cancelled):
    marker = tmp_path / "entered.json"
    script = tmp_path / "slow_resolver.py"
    script.write_text(
        "import json, os, socket, sys, time\n"
        "from pathlib import Path\n"
        "request = json.loads(sys.stdin.buffer.read())\n"
        "def resolver(*args, **kwargs):\n"
        "    Path(request['marker']).write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    raise socket.gaierror('synthetic failure')\n"
        "socket.getaddrinfo = resolver\n"
        "socket.getaddrinfo('synthetic.invalid', 80)\n", encoding="utf-8",
    )
    cancel = threading.Event()
    observations = {}

    def trigger():
        until = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < until:
            time.sleep(.01)
        observations["entered"] = marker.exists()
        if cancelled:
            observations["cancel_at"] = time.monotonic()
            cancel.set()

    observer = threading.Thread(target=trigger)
    observer.start()
    started = time.monotonic()
    expected = runtime.NetworkCancelled if cancelled else runtime.NetworkDeadlineExceeded
    try:
        with pytest.raises(expected):
            runtime._run_owned_process(
                [runtime.WORKER_PYTHON, "-I", "-B", str(script)],
                input_text=json.dumps({"marker": str(marker)}),
                deadline=started + (10 if cancelled else 1), cancel_event=cancel,
            )
    finally:
        observer.join(6)
    assert not observer.is_alive() and observations["entered"]
    assert int(marker.read_text()) == owned_processes[0].pid
    assert time.monotonic() - (observations.get("cancel_at") or started) < 3


def test_pre_cancelled_or_expired_operation_never_starts_child(owned_processes):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(runtime.NetworkCancelled):
        runtime.run_operation("dns", {"host": "synthetic.invalid"}, deadline=time.monotonic() + 5, cancel_event=cancel)
    with pytest.raises(runtime.NetworkDeadlineExceeded):
        runtime.run_operation("dns", {"host": "synthetic.invalid"}, deadline=time.monotonic() - 1)
    assert not owned_processes


def test_worker_failure_does_not_echo_private_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_run_owned_process", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 1, "", "https://user:secret@host/token"))
    with pytest.raises(runtime.NetworkWorkerError) as exc:
        runtime.run_operation("dns", {"host": "synthetic.invalid"}, deadline=time.monotonic() + 5)
    assert "secret" not in str(exc.value) and "token" not in str(exc.value)


@contextmanager
def local_http(mode):
    entered, stop = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            entered.set()
            try:
                if mode == "silent":
                    stop.wait(15)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                if mode == "success":
                    self.wfile.write(b"synthetic transport bytes")
                    return
                while not stop.is_set():
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    stop.wait(.05)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    class Server(ThreadingTCPServer):
        daemon_threads = False
        block_on_close = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/synthetic.ts", entered
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.fixture
def local_network_policy(monkeypatch):
    monkeypatch.setenv("M3U_PROFILE", "development")
    monkeypatch.setenv("M3U_ALLOW_PRIVATE_NETWORKS", "1")
    for key in list(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "m3u_outbound_proxy"}:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


def test_http_complete_success(local_network_policy, owned_processes):
    with local_http("success") as (url, entered):
        result = runtime.run_operation("http", {"entry": {"url": url}, "timeout": 1}, deadline=time.monotonic() + 10)
        assert entered.is_set() and result == [True, "OK HTTP 200", url]


@pytest.mark.parametrize("mode", ["silent", "drip"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_http_silent_and_drip_have_total_deadline_and_cancel(local_network_policy, owned_processes, mode, cancelled):
    cancel = threading.Event()
    observations = {}
    with local_http(mode) as (url, entered):
        def trigger():
            observations["entered"] = entered.wait(5)
            if cancelled:
                observations["cancel_at"] = time.monotonic()
                cancel.set()
        observer = threading.Thread(target=trigger)
        observer.start()
        started = time.monotonic()
        try:
            with pytest.raises(runtime.NetworkCancelled if cancelled else runtime.NetworkDeadlineExceeded):
                runtime.run_operation("http", {"entry": {"url": url}, "timeout": 10},
                                      deadline=started + (10 if cancelled else 1.5), cancel_event=cancel)
        finally:
            observer.join(6)
        assert not observer.is_alive() and observations["entered"]
        assert time.monotonic() - (observations.get("cancel_at") or started) < 4


def test_http_private_target_still_fails_closed(monkeypatch, owned_processes):
    monkeypatch.delenv("M3U_ALLOW_PRIVATE_NETWORKS", raising=False)
    monkeypatch.setenv("M3U_PROFILE", "development")
    result = runtime.run_operation("http", {"entry": {"url": "http://127.0.0.1:1/private"}, "timeout": 1}, deadline=time.monotonic() + 10)
    assert result[0] is False and "bloqueado" in result[1].lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_assignment_failure_reaps_child(monkeypatch, owned_processes):
    def refuse_assignment(self, process):
        raise OSError("synthetic Job assignment refusal")

    monkeypatch.setattr(runtime._WindowsJob, "assign", refuse_assignment)
    with pytest.raises(OSError, match="synthetic Job"):
        runtime._run_owned_process(
            [runtime.WORKER_PYTHON, "-I", "-B", "-c", "import time; time.sleep(30)"],
            deadline=time.monotonic() + 5,
        )
    assert len(owned_processes) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_controller_crash_closes_assigned_worker(tmp_path):
    """Kill only our controller; its non-inherited Job handle must kill its worker."""
    import ctypes
    from ctypes import wintypes

    ready = tmp_path / "assigned.json"
    controller_script = tmp_path / "controller.py"
    controller_script.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(runtime.__file__).resolve().parent)!r})\n"
        "import network_runtime as runtime\n"
        "assign = runtime._WindowsJob.assign\n"
        "def observe(self, process):\n"
        "    assign(self, process)\n"
        f"    Path({str(ready)!r}).write_text(json.dumps({{'controller': os.getpid(), 'worker': process.pid}}))\n"
        "runtime._WindowsJob.assign = observe\n"
        "runtime._run_owned_process([runtime.WORKER_PYTHON, '-I', '-B', '-c', 'import time; time.sleep(30)'], deadline=time.monotonic()+25)\n",
        encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    controller = subprocess.Popen([runtime.WORKER_PYTHON, "-I", "-B", str(controller_script)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
    child_handle = None
    try:
        until = time.monotonic() + 8
        while not ready.exists() and controller.poll() is None and time.monotonic() < until:
            time.sleep(.01)
        assert ready.exists(), "Controller did not assign its worker"
        evidence = json.loads(ready.read_text())
        assert evidence["controller"] == controller.pid, "Python redirector introduced an unowned controller"
        child_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, evidence["worker"])
        assert child_handle, "Could not retain exact owned child process handle"
        assert kernel.WaitForSingleObject(child_handle, 0) == 258, "Child already exited before simulated crash"
        controller.kill()
        controller.communicate(timeout=5)
        assert kernel.WaitForSingleObject(child_handle, 3000) == 0, "Job worker survived controller crash"
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=5)
        if child_handle:
            if kernel.WaitForSingleObject(child_handle, 0) == 258:
                kernel.TerminateProcess(child_handle, 1)
                kernel.WaitForSingleObject(child_handle, 3000)
            kernel.CloseHandle(child_handle)

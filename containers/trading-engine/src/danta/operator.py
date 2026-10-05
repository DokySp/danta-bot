"""Private local operator transport to the process that owns the account writer."""
from __future__ import annotations

from datetime import date
import fcntl
import json
import os
import socket
import stat
import struct
import threading
import time

from .config import HumanRequired, canonical, utcnow
from .daily_report import collect_daily_report
from .reporting import write_report


COMMAND_FIELDS = {
    "run": {"kind", "event_id", "request_key"}, "reconcile": set(), "status": set(),
    "pause": set(), "report": {"date"}, "candidates": {"action", "ticker"},
    "resume": {"approval"}, "activate": {"approval", "expected_config"},
}


def operator_request(args, config):
    return {"command": args.command, "config_hash": config.config_hash,
            **{key: getattr(args, key) for key in COMMAND_FIELDS[args.command]}}


def execute_operator(app, request, *, clock=utcnow):
    command = request.get("command")
    if command not in COMMAND_FIELDS or set(request) != COMMAND_FIELDS[command] | {"command", "config_hash"}:
        raise ValueError("INVALID_LOCAL_OPERATOR_REQUEST")
    # Reuse the runtime's narrowly allowed model reload contract. A different
    # trading policy must still fail before any local operation can run.
    current = app.config._current_source()
    accepted_hashes = {value for value in (app.config.config_hash, app.config.source_config_hash,
                                          current.config_hash) if value}
    if request["config_hash"] not in accepted_hashes:
        raise HumanRequired("Local operator configuration differs from the running application")
    if any(value is not None and not isinstance(value, str) for value in request.values()):
        raise ValueError("INVALID_LOCAL_OPERATOR_ARGUMENT")
    if command == "run":
        if request["kind"] not in {"full_review", "event_review"}:
            raise ValueError("INVALID_REVIEW_KIND")
        if app.config.app["monitoring"]["enabled"]:
            app.start_monitor(app.config.app["monitoring"]["quote_poll_fallback_seconds"])
        return app.review(kind=request["kind"], event_id=request["event_id"], request_key=request["request_key"])
    if command == "reconcile":
        return app.reconcile()
    if command == "status":
        return app.status()
    if command == "pause":
        return app.pause()
    if command == "candidates":
        commands = {"add": "add_portfolio_ticker", "remove": "remove_portfolio_ticker",
                    "exclude": "add_portfolio_except_ticker", "include": "remove_portfolio_except_ticker"}
        if request["action"] not in commands:
            raise ValueError("INVALID_CANDIDATE_ACTION")
        return app.update_candidate_list(commands[request["action"]], request["ticker"])
    if command == "report":
        day = date.fromisoformat(request["date"]) if request["date"] else None
        data = collect_daily_report(app, now=clock(), day=day)
        directory = app.config.state_dir / "reports" / data["date"]
        directory.mkdir(parents=True, exist_ok=True)
        return write_report(data, directory / "daily.json", directory / "daily.html", "일일 판단·성과")
    approval = app.approval
    if approval is None or approval["id"] != request["approval"]:
        raise HumanRequired("Matching trusted operator approval required")
    if command == "activate":
        app.activate(request["expected_config"])
    else:
        app.resume()
    return {"status": command.upper(), "approval_id": approval["id"]}


def _directory(config, *, create=False):
    directory = config.state_dir / "operator"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HumanRequired("Local operator directory is not private")
    return directory


def _socket_info(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HumanRequired("Local operator socket is not private")
    return info


def _check_peer(connection):
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    if uid != os.getuid():
        raise HumanRequired("Local operator peer UID is not allowed")


class OperatorLease:
    """Hold from before application creation until after its writer is closed."""
    def __init__(self, config):
        self.directory = _directory(config, create=True)
        self.fd = os.open(self.directory / "owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HumanRequired("Local operator owner lock is not private")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as error:
            self.close()
            if isinstance(error, BlockingIOError):
                raise HumanRequired("LOCAL_OPERATOR_UNAVAILABLE: owner is starting or stopping; no standalone fallback") from None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class RemoteOperatorError(Exception):
    def __init__(self, response):
        self.response = response
        super().__init__(response.get("reason", "Local operator request failed"))


def _receive(connection, limit):
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(min(65536, limit + 1 - len(data)))
        if not chunk:
            raise OSError("Local operator response ended without a complete message")
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("LOCAL_OPERATOR_MESSAGE_TOO_LARGE")
    value = json.loads(bytes(data).split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise ValueError("INVALID_LOCAL_OPERATOR_MESSAGE")
    return value


def connect_or_claim(config, request, *, timeout=None):
    """Return (remote result, None), or (None, standalone lease) if no runtime owns it."""
    path = _directory(config) / "control.sock"
    if not path.exists() and not path.is_symlink():
        lease = OperatorLease(config)
        if not path.exists() and not path.is_symlink():
            return None, lease
        lease.close()
    _socket_info(path)
    model = config.app["model"]
    timeout = timeout if timeout is not None else (
        model["timeout_seconds"] * (1 + model["transient_retries"] + model["schema_repair_attempts"]) + 30)
    sent = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            _check_peer(connection)
            sent = True  # sendall can fail after delivering part or all of the request.
            connection.sendall(canonical(request).encode() + b"\n")
            response = _receive(connection, 8 * 1024 * 1024)
    except (OSError, ValueError) as error:
        state = "LOCAL_OPERATOR_OUTCOME_UNKNOWN" if sent else "LOCAL_OPERATOR_UNAVAILABLE"
        raise HumanRequired(f"{state}: {type(error).__name__}; no standalone fallback or automatic retry") from None
    if response.get("ok") is True and "result" in response:
        return response["result"], None
    if response.get("ok") is False and response.get("exit_code") in {1, 2}:
        raise RemoteOperatorError(response)
    raise HumanRequired("LOCAL_OPERATOR_OUTCOME_UNKNOWN: invalid response; no standalone fallback or automatic retry")


class OperatorServer:
    """Drain this listener before closing Application; release lease only afterwards."""
    def __init__(self, app, *, lease=None, clock=utcnow):
        self.app, self.clock = app, clock
        self.lease = lease or OperatorLease(app.config)
        self.path = self.lease.directory / "control.sock"
        self.listener = None
        self.thread = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.active = set()
        self.identity = None

    def start(self):
        if self.path.exists() or self.path.is_symlink():
            previous = _socket_info(self.path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.5)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    current = _socket_info(self.path)
                    if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                        raise HumanRequired("Local operator socket changed during startup")
                    self.path.unlink()
                else:
                    raise HumanRequired("Local operator listener is already active")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            info = self.path.lstat()
            self.identity = (info.st_dev, info.st_ino)
            self.listener.listen(16)
            self.listener.settimeout(.2)
            self.thread = threading.Thread(target=self._accept, name="danta-operator", daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise
        return self

    def _accept(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                if self.stopping.is_set():
                    connection.close()
                    break
                thread = threading.Thread(target=self._handle, args=(connection,), daemon=True)
                self.active.add(thread)
                thread.start()

    def _handle(self, connection):
        try:
            with connection:
                connection.settimeout(5)
                try:
                    _check_peer(connection)
                    request = _receive(connection, 65536)
                    result = execute_operator(self.app, request, clock=self.clock)
                    response = {"ok": True, "result": result}
                except HumanRequired as error:
                    response = {"ok": False, "exit_code": 2, "status": error.state, "reason": str(error)}
                except Exception as error:
                    from .service import failure_detail
                    response = {"ok": False, "exit_code": 1, "status": "FAILED",
                                **failure_detail(error, stage="LOCAL_OPERATOR", now=self.clock)}
                try:
                    connection.sendall(canonical(response).encode() + b"\n")
                except OSError:
                    pass  # The operation may have committed; never execute it again here.
        finally:
            with self.lock:
                self.active.discard(threading.current_thread())

    def close(self, *, timeout=20):
        self.stopping.set()
        if self.listener:
            self.listener.close()
        deadline = time.monotonic() + timeout
        if self.thread:
            self.thread.join(max(0, deadline - time.monotonic()))
        with self.lock:
            active = list(self.active)
        for thread in active:
            thread.join(max(0, deadline - time.monotonic()))
        with self.lock:
            if self.active or (self.thread and self.thread.is_alive()):
                raise HumanRequired("Local operator request still running; retain writer and owner locks until it exits")
        if self.identity is not None:
            try:
                current = self.path.lstat()
                if (current.st_dev, current.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass

"""Kör en Minecraft-server som en vanlig java-process på noden.

Docker-noder startar containrar; en nod som är registrerad med
`runtime_kind=process` (t.ex. en Windows-box utan Docker) kör java direkt i
stället. Mastern skickar då ett `process`-block i stället för `container`:
`java_major`, `native_binary`, `argv`, `port`, `memory_mb` och `cpus`.

Samma säkerhetsregler som Docker-vägen gäller:

* `argv` är en validerad lista och körs aldrig genom ett skal (AGENTS §1) —
  mastern har redan tokeniserat startkommandot.
* Arbetskatalogen är serverns staged data-katalog, kontrollerad mot
  `CRAFTPILOT_SERVER_DATA_ROOT` så ingen process kan starta utanför roten.
* Java-binären hämtas ur en operatörskonfigurerad tabell per huvudversion
  (`CRAFTPILOT_JAVA_BINARIES`), aldrig ur `PATH`: en process-nod ska köra den
  JRE mastern valde, inte den som råkade ligga först i miljön.

Serverstatus och exit-koder sparas i `processes.json` i agentens state-katalog,
så en omstartad agent fortfarande vet vilka servrar som ägs här. Exit-koden kan
bara fångas medan agenten själv håller processhandtaget; efter en agentomstart
rapporteras `None` och servern blir OFFLINE i stället för att gissas som krasch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("craftpilot.runtime_agent")

PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
NATIVE_BINARY_RE = re.compile(r"^\./[A-Za-z0-9._-]+$")
LOG_WINDOW = 400
LOG_LINE_LIMIT = 8192
LOG_READ_LIMIT = 1024 * 1024


class ProcessError(RuntimeError):
    """Ett process-kommando som mastern bad om gick inte att utföra säkert."""


class ProcessSupervisor:
    """Äger processerna för en nod: start, stopp, konsol, logg och exit-koder."""

    def __init__(self, config: Any, popen=None, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.state_dir = Path(config.state_dir)
        self.path = self.state_dir / "processes.json"
        self.log_dir = self.state_dir / "process-logs"
        self._popen = popen or subprocess.Popen
        self._clock = clock
        self._sleep = sleep
        self._handles: dict[str, Any] = {}

    # -- bestånd ---------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"servers": {}, "cursors": {}}
        except OSError as exc:
            raise ProcessError("process state file is unreadable") from exc
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProcessError("process state file is invalid") from exc
        if not isinstance(state, dict):
            raise ProcessError("process state file has an invalid shape")
        servers, cursors = state.get("servers", {}), state.get("cursors", {})
        if not isinstance(servers, dict) or not isinstance(cursors, dict):
            raise ProcessError("process state file has an invalid shape")
        return {"servers": servers, "cursors": cursors}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".processes-", dir=self.state_dir)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # -- uppslag ---------------------------------------------------------

    def is_process_server(self, server_id: Any) -> bool:
        if not isinstance(server_id, str):
            return False
        return server_id in self._load()["servers"]

    def server_ids(self) -> list[str]:
        return sorted(self._load()["servers"])

    def record(self, server_id: str) -> dict[str, Any] | None:
        record = self._load()["servers"].get(server_id)
        return record if isinstance(record, dict) else None

    def status(self, server_id: str) -> tuple[str, int | None]:
        """("running", None) | ("exited", exit_code) | ("missing", None)."""
        state = self._load()
        record = state["servers"].get(server_id)
        if not isinstance(record, dict):
            return "missing", None
        exit_code = record.get("exit_code")
        if isinstance(exit_code, bool):
            exit_code = None
        handle = self._handles.get(server_id)
        if handle is not None:
            code = handle.poll()
            if code is not None:
                exit_code = int(code)
                if record.get("exit_code") != exit_code:
                    record["exit_code"] = exit_code
                    self._save(state)
                return "exited", exit_code
        if self._alive(record.get("pid")):
            return "running", None
        return "exited", exit_code

    # -- livscykel -------------------------------------------------------

    def start(self, server_id: str, spec: dict[str, Any], data_path: Path) -> dict[str, Any]:
        if not isinstance(server_id, str) or not server_id:
            raise ProcessError("process server id is invalid")
        state = self._load()
        existing = state["servers"].get(server_id)
        if isinstance(existing, dict) and self._alive(existing.get("pid")):
            LOGGER.info("server_process_start_noop server_id=%s reason=already_running", server_id)
            return {"success": True, "result_code": "ALREADY_RUNNING",
                    "container_id": self._process_id(server_id)}

        argv, java_major = self._command(spec, server_id)
        memory = self._int_field(spec.get("memory_mb"), "memory_mb", 512, 131072)
        port = self._int_field(spec.get("port"), "port", 1, 65535)
        self._cpu_field(spec.get("cpus"))
        directory = self._server_directory(data_path)

        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = self.log_dir / f"{server_id}.log"
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(directory),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            # Ingen ärver JVM-flaggor från agentens egen miljö: argv är hela sanningen.
            "JAVA_TOOL_OPTIONS": "",
        }
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            handle = self._popen(
                argv, cwd=str(directory), stdin=subprocess.PIPE, stdout=descriptor,
                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True, env=environment,
            )
        except OSError as exc:
            raise ProcessError("server process could not be spawned") from exc
        finally:
            os.close(descriptor)

        self._handles[server_id] = handle
        # En JVM som dör direkt (fel jar, fel JRE) ska synas i mastern som ett
        # misslyckat startförsök — inte som en server som "kör".
        self._sleep(0.2)
        if handle.poll() is not None:
            self._handles.pop(server_id, None)
            raise ProcessError(f"server process exited immediately with code {handle.returncode}")

        state["servers"][server_id] = {
            "pid": getattr(handle, "pid", None),
            "started_at": _timestamp(datetime.now(timezone.utc)),
            "java_major": java_major,
            "command": argv,
            "log": str(log_path),
            "port": port,
            "memory_limit_mb": memory,
            "exit_code": None,
        }
        state["cursors"].pop(server_id, None)
        self._save(state)
        LOGGER.info("server_process_started server_id=%s java_major=%s", server_id, java_major)
        return {"success": True, "result_code": "STARTED", "container_id": self._process_id(server_id)}

    def stop(self, server_id: str, force: bool) -> dict[str, Any]:
        state = self._load()
        record = state["servers"].get(server_id)
        if not isinstance(record, dict):
            return {"success": True, "result_code": "ALREADY_STOPPED"}
        handle = self._handles.get(server_id)
        live = self._alive(record.get("pid")) or (handle is not None and handle.poll() is None)
        if not live:
            self._forget(state, server_id, handle)
            return {"success": True, "result_code": "ALREADY_STOPPED"}

        if force:
            LOGGER.warning("server_process_force_stop_requested server_id=%s", server_id)
            self._terminate(handle, record.get("pid"), force=True)
        else:
            LOGGER.info("server_process_graceful_stop_requested server_id=%s", server_id)
            if not self._write_stdin(handle, "stop\n"):
                # Ingen stdin (t.ex. efter en agentomstart): be processen stänga
                # av med SIGTERM i stället, aldrig med SIGKILL direkt.
                self._terminate(handle, record.get("pid"), force=False)
            for _ in range(int((self.config.stop_grace_seconds + 15) * 2)):
                if handle is not None and handle.poll() is not None:
                    break
                if handle is None and not self._alive(record.get("pid")):
                    break
                self._sleep(0.5)
            else:
                self._terminate(handle, record.get("pid"), force=False)

        self._forget(state, server_id, handle)
        LOGGER.info("server_process_stopped server_id=%s method=%s", server_id, "force" if force else "graceful")
        return {"success": True, "result_code": "STOPPED"}

    def delete(self, server_id: str) -> None:
        """Stoppa servern om den kör och glöm bort process, logg och cursor.

        Anropas när mastern tar bort servern. Idempotent: en server som redan
        är borta ger inget fel. Loggfilen tas bort efter posten så en senare
        rapport inte kan peka på en halvstädad server.
        """
        if self.is_process_server(server_id):
            status, _exit_code = self.status(server_id)
            if status == "running":
                self.stop(server_id, force=True)
        state = self._load()
        record = state["servers"].pop(server_id, None)
        state["cursors"].pop(server_id, None)
        self._save(state)
        self._handles.pop(server_id, None)
        log_path = record.get("log") if isinstance(record, dict) else None
        if isinstance(log_path, str):
            try:
                Path(log_path).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ProcessError("process log could not be removed") from exc

    def console(self, server_id: str, command: Any) -> None:
        if (not isinstance(command, str) or not command or len(command) > 16384
                or "\x00" in command or "\n" in command or "\r" in command):
            raise ProcessError("console command is invalid")
        status, _exit_code = self.status(server_id)
        if status != "running":
            raise ProcessError("server process is not running")
        handle = self._handles.get(server_id)
        if handle is None or getattr(handle, "stdin", None) is None:
            raise ProcessError("server process console is not attached to this agent")
        if not self._write_stdin(handle, command + "\n"):
            raise ProcessError("server process rejected the console command")

    # -- logg ------------------------------------------------------------

    def logs(self, server_id: str, since: str | None = None) -> list[dict[str, str]]:
        state = self._load()
        record = state["servers"].get(server_id)
        if not isinstance(record, dict) or not isinstance(record.get("log"), str):
            return []
        offset = 0
        cursor = state["cursors"].get(server_id)
        if (isinstance(cursor, dict) and isinstance(since, str)
                and cursor.get("ts") == since and isinstance(cursor.get("offset"), int)):
            offset = max(0, cursor["offset"])
        try:
            with open(record["log"], "rb") as log_file:
                log_file.seek(offset)
                data = log_file.read(LOG_READ_LIMIT)
        except OSError:
            return []
        consumed = data.rfind(b"\n") + 1
        if consumed == 0:
            return []

        raw_lines = data[:consumed].split(b"\n")[:-1]
        kept = raw_lines[-LOG_WINDOW:]
        skipped = len(raw_lines) - len(kept)
        base = datetime.now(timezone.utc)
        entries = [
            {"ts": _timestamp(base + timedelta(microseconds=index)),
             "text": line.decode("utf-8", errors="replace").rstrip("\r")[-LOG_LINE_LIMIT:]}
            for index, line in enumerate(kept)
        ]
        state["cursors"][server_id] = {
            "ts": entries[-1]["ts"],
            "offset": offset + sum(len(line) + 1 for line in raw_lines[:skipped]) + sum(
                len(line) + 1 for line in kept
            ),
        }
        self._save(state)
        return entries

    # -- invändigt -------------------------------------------------------

    def _command(self, spec: dict[str, Any], server_id: str) -> tuple[list[str], int | None]:
        raw = spec.get("command")
        if (not isinstance(raw, list) or not 1 <= len(raw) <= 64
                or any(not isinstance(part, str) or not part or len(part) > 4096 or "\x00" in part
                       for part in raw)):
            raise ProcessError("process command must be a non-empty argv list")
        native = spec.get("native_binary")
        if not isinstance(native, bool):
            raise ProcessError("process native_binary flag must be a boolean")
        if native:
            if not NATIVE_BINARY_RE.fullmatch(raw[0]):
                raise ProcessError("native binary must be a file in the server directory")
            return raw, None

        major = spec.get("java_major")
        if isinstance(major, bool) or not isinstance(major, int) or not 1 <= major <= 99:
            raise ProcessError("process java_major is outside the allowed range")
        if raw[0] != "java":
            raise ProcessError("java command must start with the java token")
        binaries = getattr(self.config, "java_binaries", None) or {}
        executable = binaries.get(major) if isinstance(binaries, dict) else None
        if not isinstance(executable, str) or not os.path.isabs(executable):
            raise ProcessError(f"no Java {major} runtime is configured on this node")
        if not os.path.isfile(executable):
            raise ProcessError(f"configured Java {major} runtime does not exist on this node")
        LOGGER.info("server_process_java_selected server_id=%s java_major=%s", server_id, major)
        return [executable, *raw[1:]], major

    def _server_directory(self, data_path: Path) -> Path:
        root = Path(self.config.data_root).resolve(strict=True)
        directory = Path(data_path)
        if directory.is_symlink() or not directory.is_dir():
            raise ProcessError("server data is not staged on this node")
        resolved = directory.resolve(strict=True)
        if resolved.parent != root:
            raise ProcessError("server data path escapes the configured root")
        return resolved

    def _forget(self, state: dict[str, Any], server_id: str, handle: Any) -> None:
        """Släpp processen men behåll posten som "stoppad".

        Posten är det enda spåret av att servern körs som process på den här
        noden. Tas den bort faller servern tillbaka på Docker-vägen vid nästa
        rapport, så kvar blir pid=None, en sista exit-kod och loggkursorn.
        """
        record = state["servers"].get(server_id)
        if isinstance(record, dict):
            if handle is not None:
                try:
                    code = handle.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    code = None
                if isinstance(code, int) and not isinstance(code, bool):
                    record["exit_code"] = code
            record["pid"] = None
            record["stopped_at"] = _timestamp(datetime.now(timezone.utc))
        self._save(state)
        self._handles.pop(server_id, None)

    def _terminate(self, handle: Any, pid: Any, force: bool) -> None:
        if handle is not None:
            try:
                if force:
                    handle.kill()
                else:
                    handle.terminate()
            except OSError as exc:
                raise ProcessError("server process could not be signalled") from exc
            if force:
                return
            deadline = self._clock() + 10
            while self._clock() < deadline:
                if handle.poll() is not None:
                    return
                self._sleep(0.2)
            handle.kill()
            return
        if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
            raise ProcessError("server process cannot be signalled after an agent restart")
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProcessError("server process could not be signalled") from exc

    @staticmethod
    def _write_stdin(handle: Any, text: str) -> bool:
        if handle is None or getattr(handle, "stdin", None) is None:
            return False
        try:
            handle.stdin.write(text.encode("utf-8"))
            handle.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True

    @staticmethod
    def _alive(pid: Any) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _process_id(server_id: str) -> str:
        return f"process-{server_id.replace('-', '')[:8]}"

    @staticmethod
    def _int_field(value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ProcessError(f"process {name} is outside the allowed range")
        return value

    @staticmethod
    def _cpu_field(value: Any) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or value != value
                or not 0.1 <= value <= 64):
            raise ProcessError("process cpus is outside the allowed range")


def _timestamp(moment: datetime) -> str:
    """ISO-8601 med nanosekunders form — samma form masterns loggkursor har."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}000Z"

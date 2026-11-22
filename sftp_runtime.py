"""SFTPGo på en runtime-nod: container, admin-API och state-synk.

Mastern äger behörigheten (se `docs/RUNTIME-NODE-SFTP.md` i CraftPilot) och
skickar hela det önskade läget. Den här modulen verkställer det på nodens egen
SFTPGo och rapporterar nodens SSH-värdnyckel, så panelen kan visa var användaren
ska ansluta och vilket fingeravtryck klienten ska förvänta sig.

Säkerhetsregler:

- Admin-API:t publiceras aldrig; bara agenten och SFTPGo ligger på det egna
  nätet. Endast SFTP-porten publiceras på värden.
- SFTPGo körs som root av samma skäl som på mastern: hemmen och
  serverkatalogerna ägs av root/33. Filerna den skapar tilldelas användarens
  uid/gid via användarobjektet.
- Admin-lösenordet genereras en gång och sparas i agentens state-katalog
  (0600). Det loggas aldrig och skrivs aldrig till agent-state.json.
- Användarlösenord finns bara i minnet mellan masterns secrets-svar och PUT
  mot SFTPGo. Revisionen sparas i minnet; klartexten skrivs aldrig till
  state-filen, loggar, statusrapporten eller claimen.
- Ett konto får aldrig sakna autentiseringsmetod: utan publik nyckel krävs
  ett tillämpat lösenord, annars tas kontot bort.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("craftpilot.runtime_agent")

DEFAULT_IMAGE = "drakkan/sftpgo:v2.6.4"
DEFAULT_NETWORK = "craftpilot-sftp"
DEFAULT_CONTAINER = "craftpilot-sftp"
DEFAULT_ADMIN_USERNAME = "craftpilot-node"
DEFAULT_ADMIN_URL = "http://craftpilot-sftp:8080"
DEFAULT_HOME_ROOT = "/sftp-home"
ADMIN_PASSWORD_FILE = "sftpgo-admin-password"

FEATURE_SFTP = "sftp"
FEATURE_SFTP_PASSWORD = "sftp-password"
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 256
MAX_STATUS_USERS = 500
MAX_SECRETS_USERS = 1000
MAX_REVISION = 4294967295

USERNAME_RE = re.compile(r"^u[1-9][0-9]{0,18}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
ADMIN_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def parse_password_secrets(payload: Any, requirements: dict[str, int]) -> dict[str, tuple[int, str]]:
    """Validera masterns engångshemligheter och behåll bara det vi behöver.

    Returvärdet finns bara i anroparens minne. Ett svar för en annan användare
    eller en annan revision ignoreras; agenten applicerar aldrig en hemlighet
    som inte matchar state. Själva lösenordet loggas aldrig.
    """

    if not isinstance(payload, dict):
        raise SftpError("SFTP secrets response is invalid")
    secrets = payload.get("secrets")
    if not isinstance(secrets, list) or len(secrets) > MAX_SECRETS_USERS:
        raise SftpError("SFTP secrets response is invalid")

    parsed: dict[str, tuple[int, str]] = {}
    for entry in secrets:
        if not isinstance(entry, dict):
            raise SftpError("SFTP secret entry is invalid")
        username = entry.get("username")
        revision = entry.get("revision")
        password = entry.get("password")
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise SftpError("SFTP secret username is invalid")
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or not 0 <= revision <= MAX_REVISION):
            raise SftpError("SFTP secret revision is invalid")
        if (not isinstance(password, str) or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH
                or "\x00" in password):
            raise SftpError("SFTP secret password is invalid")

        if username not in requirements or revision != requirements[username]:
            continue
        if username in parsed:
            raise SftpError("SFTP secrets response contains a duplicate user")
        parsed[username] = (revision, password)
    return parsed


# En container som inte svarar startas om högst så här ofta; annars skulle en
# trasig SFTPGo bilda en start-loop mot Docker.
START_BACKOFF_SECONDS = 30.0


class SftpError(RuntimeError):
    """Fel i nodens SFTPGo-drift. Meddelandet innehåller aldrig hemligheter."""


@dataclass(frozen=True)
class SftpSettings:
    """Allt modulen behöver, fristående från agentens Config."""

    state_dir: Path
    # Sökvägen som agenten ser serverdata på (används bara för kontroller).
    data_root: Path
    # Samma rot som Docker-daemonen ser den; virtual folders pekar hit.
    docker_data_root: str | None
    image: str = DEFAULT_IMAGE
    port: int = 2222
    network: str = DEFAULT_NETWORK
    container_name: str = DEFAULT_CONTAINER
    admin_url: str = DEFAULT_ADMIN_URL
    admin_username: str = DEFAULT_ADMIN_USERNAME
    home_root: str = DEFAULT_HOME_ROOT
    uid: int = 33
    gid: int = 33
    agent_container_name: str = "craftpilot-runtime-agent"


def _admin_password_path(state_dir: Path) -> Path:
    return state_dir / ADMIN_PASSWORD_FILE


def load_or_create_admin_password(state_dir: Path) -> str:
    """Läs det sparade admin-lösenordet, annars skapa ett nytt (0600).

    Ett befintligt men ogiltigt värde avvisas i stället för att roteras: den
    redan skapade SFTPGo-databasen har det gamla lösenordet, och en tyst
    rotation skulle bara göra admin-API:t oåtkomligt.
    """

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _admin_password_path(state_dir)

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        password = secrets.token_urlsafe(32)
        _write_secret_file(path, password)
        return password

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SftpError("SFTPGo admin password file is not a regular file")
        if os.name != "nt" and info.st_mode & 0o077:
            os.fchmod(descriptor, 0o600)
        raw = os.read(descriptor, 256).decode("utf-8", "strict").strip()
    except OSError as exc:
        raise SftpError("SFTPGo admin password file is unreadable") from exc
    finally:
        os.close(descriptor)

    if not ADMIN_PASSWORD_RE.fullmatch(raw):
        raise SftpError("SFTPGo admin password file is invalid")
    return raw


def _write_secret_file(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".sftpgo-admin-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def host_key_fingerprint(status: Any) -> str | None:
    """Välj en deterministisk värdnyckel ur SFTPGo:s status-svar.

    SFTPGo kan ha flera nycklar (RSA/ECDSA/Ed25519). Panelen visar en, så
    Ed25519 vinner när den finns; annars första giltiga SHA256-värdet.
    """

    if not isinstance(status, dict):
        return None
    ssh = status.get("ssh")
    if not isinstance(ssh, dict):
        return None
    keys = ssh.get("host_keys")
    if not isinstance(keys, list):
        return None

    fallback: str | None = None
    for key in keys:
        if not isinstance(key, dict):
            continue
        fingerprint = key.get("fingerprint")
        if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
            continue
        algorithms = key.get("algorithms")
        names = algorithms if isinstance(algorithms, list) else []
        if any(isinstance(name, str) and "ed25519" in name.lower() for name in names):
            return fingerprint
        if fallback is None:
            fallback = fingerprint

    return fallback


class SftpGoClient:
    """Minsta möjliga admin-klient mot nodens SFTPGo (REST v2)."""

    def __init__(self, base_url: str, username: str, password: str,
                 opener: Any | None = None, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self._opener = opener or urllib.request.build_opener()
        self.timeout = timeout
        self._token: str | None = None

    def status(self) -> dict[str, Any]:
        payload = self._call("GET", "/api/v2/status")
        if not isinstance(payload, dict):
            raise SftpError("SFTPGo returned an invalid status response")
        return payload

    def list_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        offset = 0
        for _ in range(20):
            payload = self._call("GET", f"/api/v2/users?limit=100&offset={offset}")
            if not isinstance(payload, list):
                raise SftpError("SFTPGo returned an invalid user list")
            users.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return users
            offset += len(payload)
        raise SftpError("SFTPGo returned too many users")

    def ensure_folder(self, name: str, mapped_path: str) -> None:
        payload = {"name": name, "mapped_path": mapped_path}
        path = "/api/v2/folders/" + urllib.parse.quote(name, safe="")
        if self._call("PUT", path, payload, allow_missing=True) is None:
            self._call("POST", "/api/v2/folders", payload, allow_conflict=True)

    def put_user(self, payload: dict[str, Any]) -> None:
        username = payload["username"]
        path = "/api/v2/users/" + urllib.parse.quote(username, safe="")
        if self._call("PUT", path, payload, allow_missing=True) is None:
            self._call("POST", "/api/v2/users", payload, allow_conflict=True)

    def delete_user(self, username: str) -> None:
        path = "/api/v2/users/" + urllib.parse.quote(username, safe="")
        self._call("DELETE", path, allow_missing=True)

    # -- transport ---------------------------------------------------------

    def _authenticate(self) -> str:
        if self._token is not None:
            return self._token
        credentials = base64.b64encode(
            f"{self.username}:{self._password}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            self.base_url + "/api/v2/token",
            headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"},
            method="GET",
        )
        try:
            payload = self._send(request)
        except _StatusError as exc:
            raise SftpError(f"SFTPGo authentication failed with status {exc.status}") from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise SftpError("SFTPGo did not return an access token")
        self._token = token
        return token

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None,
              allow_missing: bool = False, allow_conflict: bool = False) -> Any:
        for attempt in (0, 1):
            token = self._authenticate()
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(self.base_url + path, data=data,
                                             headers=headers, method=method)
            try:
                return self._send(request)
            except _StatusError as exc:
                if exc.status == 401 and attempt == 0:
                    self._token = None
                    continue
                if exc.status == 404 and allow_missing:
                    return None
                if exc.status == 409 and allow_conflict:
                    return {}
                raise SftpError(f"SFTPGo request failed with status {exc.status}") from None
        raise SftpError("SFTPGo authentication failed")

    def _send(self, request: urllib.request.Request) -> Any:
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _StatusError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SftpError("SFTPGo could not be reached") from exc

        if len(raw) > 4 * 1024 * 1024:
            raise SftpError("SFTPGo response exceeded the size limit")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SftpError("SFTPGo returned invalid JSON") from None


class _StatusError(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class SftpNode:
    """SFTPGo-containern på noden plus synken av masterns önskade läge."""

    def __init__(self, settings: SftpSettings, runner: Callable[..., Any] = subprocess.run,
                 client_factory: Callable[[str, str, str], SftpGoClient] | None = None,
                 docker_available: Callable[[], bool] | None = None):
        self.settings = settings
        self.runner = runner
        self._client_factory = client_factory or SftpGoClient
        self._docker_available = docker_available or (lambda: shutil.which("docker") is not None)
        self._client: SftpGoClient | None = None
        self._last_revision: str | None = None
        self._password_revisions: dict[str, int] = {}
        self._last_status: list[dict[str, Any]] = []
        self._last_start_attempt = 0.0
        self._container_restarted = False

    # -- livscykel ---------------------------------------------------------

    def ensure_ready(self) -> str | None:
        """Se till att SFTPGo svarar; returnera värdnyckelns fingeravtryck.

        `None` betyder att noden inte kan erbjuda SFTP just nu — agenten ska
        då inte rapportera `sftp` i sina features, så panelen inte lovar en
        slutpunkt som inte fungerar.
        """

        if not self.settings.docker_data_root:
            LOGGER.warning("sftp_docker_data_root_missing sftp_disabled")
            return None
        if not self._docker_available():
            return None

        try:
            self._ensure_network()
            self._ensure_container()
            self._ensure_agent_network()
            status = self._client_instance().status()
        except (SftpError, OSError, TimeoutError) as exc:
            LOGGER.warning("sftp_not_ready error=%s", str(exc)[:200])
            self._restart_container()
            return None

        fingerprint = host_key_fingerprint(status)
        if fingerprint is None:
            LOGGER.warning("sftp_status_without_host_key")
            return None
        return fingerprint

    def _client_instance(self) -> SftpGoClient:
        if self._client is None:
            password = load_or_create_admin_password(self.settings.state_dir)
            self._client = self._client_factory(
                self.settings.admin_url, self.settings.admin_username, password,
            )
        return self._client

    def _docker(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return self.runner(args, capture_output=True, text=True, timeout=timeout, check=False)

    def _ensure_network(self) -> None:
        network = self.settings.network
        result = self._docker(["docker", "network", "inspect", network], timeout=10)
        if result.returncode == 0:
            return
        result = self._docker(["docker", "network", "create", network], timeout=20)
        if result.returncode != 0 and "already exists" not in (result.stderr or "").lower():
            raise SftpError("could not create the SFTP network")

    def _ensure_agent_network(self) -> None:
        network = self.settings.network
        agent = self.settings.agent_container_name
        result = self._docker([
            "docker", "inspect", "--format",
            f'{{{{if index .NetworkSettings.Networks "{network}"}}}}attached{{{{end}}}}',
            agent,
        ], timeout=10)
        if result.returncode != 0:
            raise SftpError("could not inspect the agent network membership")
        if result.stdout.strip() == "attached":
            return
        result = self._docker(["docker", "network", "connect", network, agent], timeout=10)
        if result.returncode != 0 and "already exists" not in (result.stderr or "").lower():
            raise SftpError("could not attach the agent to the SFTP network")

    def _container_running(self) -> bool | None:
        result = self._docker([
            "docker", "inspect", "--format", "{{.State.Running}}",
            self.settings.container_name,
        ], timeout=10)
        if result.returncode != 0:
            return None
        return result.stdout.strip() == "true"

    def _ensure_container(self) -> None:
        running = self._container_running()
        if running is True:
            return
        if running is False:
            result = self._docker(["docker", "start", self.settings.container_name], timeout=30)
            if result.returncode != 0:
                raise SftpError("could not start the SFTPGo container")
            self._container_restarted = True
            return
        self._create_container()
        self._container_restarted = True

    def _create_container(self) -> None:
        settings = self.settings
        assert settings.docker_data_root is not None
        password = load_or_create_admin_password(settings.state_dir)
        data_volume = f"{settings.container_name}-data"
        home_volume = f"{settings.container_name}-home"
        data_root = settings.docker_data_root.rstrip("/\\")
        if not data_root or CONTROL_RE.search(data_root):
            raise SftpError("Docker server data root is invalid")

        args = [
            "docker", "run", "--detach",
            "--name", settings.container_name,
            "--restart", "unless-stopped",
            "--network", settings.network,
            # Root av samma skäl som på mastern: hemmen och serverkatalogerna
            # ägs av root/33. Filerna får användarens uid/gid via payloaden.
            "--user", "0:0",
            "-p", f"{settings.port}:2022",
            "-v", f"{data_volume}:/var/lib/sftpgo",
            "-v", f"{home_volume}:{settings.home_root}",
            "-v", f"{data_root}:{data_root}",
            "-e", "SFTPGO_DATA_PROVIDER=sqlite",
            "-e", "SFTPGO_SQLITE_NAME=/var/lib/sftpgo/sftpgo.db",
            "-e", "SFTPGO_HTTPD_BIND_ADDRESS=0.0.0.0",
            "-e", "SFTPGO_HTTPD_PORT=8080",
            "-e", "SFTPGO_SFTPD_BIND_ADDRESS=0.0.0.0",
            "-e", "SFTPGO_SFTPD_PORT=2022",
            "-e", "SFTPGO_DATA_PROVIDER__CREATE_DEFAULT_ADMIN=true",
            "-e", f"SFTPGO_DEFAULT_ADMIN_USERNAME={settings.admin_username}",
            "-e", f"SFTPGO_DEFAULT_ADMIN_PASSWORD={password}",
            settings.image,
        ]
        result = self._docker(args, timeout=120)
        if result.returncode != 0:
            raise SftpError("could not create the SFTPGo container")
        # En ny databas har inga lösenord; gamla minnesrevisioner får inte
        # hindra att masterns hemligheter hämtas igen.
        self._password_revisions.clear()
        self._last_status = []

    def _restart_container(self) -> None:
        """Ett startförsök per backoff-fönster när healthchecken fallerar."""

        now = time.monotonic()
        if now - self._last_start_attempt < START_BACKOFF_SECONDS:
            return
        self._last_start_attempt = now
        if self._container_running() is False:
            result = self._docker(["docker", "start", self.settings.container_name], timeout=30)
            if result.returncode == 0:
                self._container_restarted = True
                LOGGER.info("sftp_container_restarted")

    # -- state-synk --------------------------------------------------------

    def password_requirements(self, state: Any) -> dict[str, int]:
        """Användare vars lokala lösenordsrevision är äldre än masterns.

        Efter en lyckad applicering (även med tom secrets-lista) är revisionen
        cachad och inget nytt hämtas förrän state ändras. Det håller
        secrets-endpointens budget och undviker att TTL-missar blir en loop.
        """

        revision, users = self._parse_state(state)
        if revision == self._last_revision and not self._container_restarted:
            return {}

        requirements: dict[str, int] = {}
        for entry in users:
            if not entry["password_set"]:
                continue
            state_revision = entry["password_revision"]
            if state_revision > self._password_revisions.get(entry["username"], 0):
                requirements[entry["username"]] = state_revision
        return requirements

    def apply_state(self, state: Any, assigned_server_ids: set[str],
                    resolve_server_path: Callable[[str], str],
                    passwords: dict[str, tuple[int, str]] | None = None,
                    passwords_complete: bool = True) -> list[dict[str, Any]]:
        """Applicera masterns fullständiga önskade läge, idempotent.

        `passwords` är username -> (revision, klartext) och finns bara i
        anropets minne. Returvärdet är kvittensrader (username + revision),
        aldrig lösenord. `passwords_complete=False` betyder att hemligheterna
        inte kunde hämtas; då cachas inte revisionen så nästa poll försöker
        igen, men nyckelbaserade konton fungerar under tiden.
        """

        revision, users = self._parse_state(state)
        if revision == self._last_revision and not self._container_restarted:
            return list(self._last_status)

        client = self._client_instance()
        secrets = passwords or {}
        folders: dict[str, str] = {}
        prepared: list[dict[str, Any]] = []
        status_reports: list[dict[str, Any]] = []
        next_revisions: dict[str, int] = {}

        for entry in users:
            username = entry["username"]
            public_keys = entry["public_keys"]
            state_revision = entry["password_revision"]
            local_revision = self._password_revisions.get(username, 0)
            password_value: str | None = None

            if entry["password_set"]:
                if local_revision >= state_revision:
                    # Redan applicerad: utelämnat password bevarar SFTPGo:s hash.
                    local_revision = max(local_revision, state_revision)
                elif not passwords_complete and local_revision > 0:
                    # Tillfälligt secrets-fel: behåll det fungerande lösenordet
                    # och rapportera den äldre revisionen. Nästa poll försöker
                    # igen; först ett bekräftat tomt svar får rensa.
                    pass
                else:
                    secret = secrets.get(username)
                    if secret is not None and secret[0] == state_revision:
                        password_value = secret[1]
                        local_revision = state_revision
                    else:
                        # Ingen användbar hemlighet (TTL, kanal av, fel revision):
                        # ta bort ett gammalt lösenord i stället för att ljuga.
                        password_value = ""
                        local_revision = 0
            else:
                # Clear/disable kräver ingen hemlighet. Revisionen höjs även
                # när lösenordet tas bort, så noden måste kvittera den.
                if state_revision > 0 or local_revision > 0:
                    password_value = ""
                local_revision = state_revision

            has_password = (password_value not in (None, "")) or (
                entry["password_set"] and local_revision > 0
            )
            if not public_keys and not has_password:
                # Aldrig ett konto utan autentiseringsmetod. Ett befintligt
                # konto tas bort av städningen nedan, och revisionen rapporteras
                # så panelen kan säga att lösenordet saknas.
                if entry["password_set"] or state_revision > 0 or local_revision > 0:
                    status_reports.append({
                        "username": username,
                        "password_revision": local_revision,
                    })
                continue

            virtual_folders = []
            permissions: dict[str, list[str]] = {"/": ["list"]}

            for folder in entry["folders"]:
                name, server_id = folder["name"], folder["server_id"]
                if server_id.lower() not in assigned_server_ids:
                    raise SftpError("SFTP state references a server not assigned to this node")
                mapped_path = resolve_server_path(server_id)
                self._validate_mapped_path(mapped_path)
                if folders.setdefault(name, mapped_path) != mapped_path:
                    raise SftpError("SFTP folder maps to more than one server path")
                virtual_folders.append({"name": name, "virtual_path": f"/{name}"})
                permissions[f"/{name}"] = ["*"]

            payload = {
                "status": 1 if entry["enabled"] else 0,
                "username": username,
                "home_dir": f"{self.settings.home_root}/{username}",
                "uid": self.settings.uid,
                "gid": self.settings.gid,
                "permissions": permissions,
                "public_keys": public_keys,
                "virtual_folders": virtual_folders,
            }
            # password utelämnas när det redan är applicerat; tom sträng rensar
            # (verifierat mot SFTPGo 2.6.4 och masterns SftpUserSpec).
            if password_value is not None:
                payload["password"] = password_value
            prepared.append(payload)
            next_revisions[username] = local_revision
            if entry["password_set"] or state_revision > 0 or local_revision > 0:
                status_reports.append({
                    "username": username,
                    "password_revision": local_revision,
                })

        for name, mapped_path in folders.items():
            client.ensure_folder(name, mapped_path)

        for payload in prepared:
            client.put_user(payload)

        desired = {payload["username"] for payload in prepared}
        for existing in client.list_users():
            username = existing.get("username")
            if isinstance(username, str) and USERNAME_RE.fullmatch(username) and username not in desired:
                client.delete_user(username)
                next_revisions.pop(username, None)

        if len(status_reports) > MAX_STATUS_USERS:
            LOGGER.warning("sftp_status_too_many_users count=%d", len(status_reports))
            status_reports = status_reports[:MAX_STATUS_USERS]

        self._password_revisions = next_revisions
        self._last_status = status_reports
        if passwords_complete:
            self._last_revision = revision
            self._container_restarted = False
        return list(status_reports)

    @staticmethod
    def _parse_state(state: Any) -> tuple[str, list[dict[str, Any]]]:
        if not isinstance(state, dict):
            raise SftpError("SFTP state is invalid")
        revision = state.get("revision")
        users = state.get("users")
        if not isinstance(revision, str) or not revision or len(revision) > 128:
            raise SftpError("SFTP state revision is invalid")
        if not isinstance(users, list) or len(users) > 1000:
            raise SftpError("SFTP state users are invalid")

        parsed: list[dict[str, Any]] = []
        for entry in users:
            if not isinstance(entry, dict):
                raise SftpError("SFTP state user is invalid")
            username = entry.get("username")
            if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
                raise SftpError("SFTP state username is invalid")
            enabled = entry.get("enabled")
            if not isinstance(enabled, bool):
                raise SftpError("SFTP state enabled flag is invalid")

            password_set = entry.get("password_set", False)
            if not isinstance(password_set, bool):
                raise SftpError("SFTP state password flag is invalid")
            password_revision = entry.get("password_revision", 0)
            if (isinstance(password_revision, bool) or not isinstance(password_revision, int)
                    or not 0 <= password_revision <= MAX_REVISION):
                raise SftpError("SFTP state password revision is invalid")

            public_keys = entry.get("public_keys")
            if not isinstance(public_keys, list) or len(public_keys) > 100:
                raise SftpError("SFTP state public keys are invalid")
            keys: list[str] = []
            for key in public_keys:
                if (not isinstance(key, str) or not 8 <= len(key) <= 4096
                        or CONTROL_RE.search(key)
                        or not key.split(" ", 1)[0].startswith(("ssh-", "sk-"))):
                    raise SftpError("SFTP state public key is invalid")
                keys.append(key)

            raw_folders = entry.get("folders")
            if not isinstance(raw_folders, list) or not raw_folders:
                raise SftpError("SFTP state folders are invalid")
            folders: list[dict[str, str]] = []
            seen: set[str] = set()
            for folder in raw_folders[:1000]:
                if not isinstance(folder, dict):
                    raise SftpError("SFTP state folder is invalid")
                name = folder.get("name")
                server_id = folder.get("server_id")
                if (not isinstance(name, str) or not SLUG_RE.fullmatch(name) or name in (".", "..")
                        or name in seen):
                    raise SftpError("SFTP state folder name is invalid")
                if not isinstance(server_id, str) or not is_uuid(server_id):
                    raise SftpError("SFTP state folder server id is invalid")
                seen.add(name)
                folders.append({"name": name, "server_id": server_id})

            parsed.append({
                "username": username,
                "enabled": enabled,
                "public_keys": keys,
                "password_set": password_set,
                "password_revision": password_revision,
                "folders": folders,
            })
        return revision, parsed

    def _validate_mapped_path(self, mapped_path: Any) -> None:
        root = (self.settings.docker_data_root or "").rstrip("/")
        if (not isinstance(mapped_path, str) or not mapped_path.startswith("/")
                or CONTROL_RE.search(mapped_path) or ".." in mapped_path.split("/")):
            raise SftpError("SFTP folder path is invalid")
        if not root or not mapped_path.startswith(root + "/"):
            raise SftpError("SFTP folder path is outside the server data root")


def is_uuid(value: str) -> bool:
    return bool(re.fullmatch(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value, re.I,
    ))


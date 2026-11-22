"""Kumulativa nätverksräknare för en container, via Docker Engine API.

Kontraktet finns i CraftPilot `docs/RUNTIME-NODE-NETWORK-TELEMETRY.md`: agenten
skickar containerns råa byte-räknare och aldrig en hastighet, eftersom mastern
äger tiden mellan rapporterna och reglerna för när en siffra får visas.

`docker stats`-CLI:t duger inte som källa: dess `NetIO` är avrundat till tre
värdesiffror med oklar enhet, så ett delta på några kilobyte blir antingen noll
eller en spik. Stats-endpointen ger råa heltal per interface, och alla interface
summeras — en server kan sitta på både spelarnas nät och proxyns, och då
underskattar en enskild `eth0` trafiken.

Modulen använder bara stdlib; agenten har inga tredjepartsberoenden.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket

DEFAULT_SOCKET_PATH = "/var/run/docker.sock"
# Golv som alla daemoner >= Docker 20.10 accepterar. Höj den om agenten redan
# pinnar en nyare API-version.
DEFAULT_API_VERSION = "v1.41"
# Kort tidsgräns: hellre null i rapporten än en försenad rapport.
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 1 << 20
# Osignerat 64-bitars, samma tak som masterns validering.
MAX_COUNTER = 2**64 - 1
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$", re.I)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP över Docker-socketen (AF_UNIX), utan tredjepartsbibliotek."""

    def __init__(self, path: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        # En Windows-Python utan AF_UNIX (process-nod utan Docker) ska ge
        # "ingen mätning", inte en kraschad rapport.
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("this platform has no unix-domain sockets")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def docker_endpoint(environ: dict[str, str] | os._Environ[str] | None = None) -> tuple[str, str] | None:
    """Docker-adressen som (typ, adress), eller None när den inte kan användas.

    `DOCKER_HOST` får vara `unix:///…` eller `tcp://…`/`http://…`. Windows
    använder `npipe://` för Docker Desktop, men den agenten kör i WSL eller i en
    Linux-container där socketen är en fil — hellre ingen mätning än en gissning.
    """
    source = os.environ if environ is None else environ
    value = (source.get("DOCKER_HOST") or "").strip()
    if not value:
        return "unix", DEFAULT_SOCKET_PATH
    scheme, separator, address = value.partition("://")
    if not separator or not address:
        return None
    if scheme == "unix":
        return "unix", address
    if scheme in ("tcp", "http"):
        return "tcp", address.rstrip("/")
    return None


def _connection(environ, timeout: float):
    endpoint = docker_endpoint(environ)
    if endpoint is None:
        return None
    kind, address = endpoint
    if kind == "unix":
        return _UnixHTTPConnection(address, timeout)
    return http.client.HTTPConnection(address, timeout=timeout)


def _last_document(raw: bytes) -> object | None:
    """Sista JSON-objektet i svaret.

    Utan `one-shot` kan daemonen skicka två mätningar i följd; den sista är den
    färdiga. Allt som inte går att läsa lämnas som None i stället för att gissa.
    """
    text = raw.decode("utf-8", "replace")
    decoder = json.JSONDecoder()
    last: object | None = None
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            document, index = decoder.raw_decode(text, index)
        except ValueError:
            return last
        last = document
    return last


def _sum_networks(networks: object) -> tuple[int, int] | None:
    """Summan av alla interface, eller None när inget kunde mätas."""
    if not isinstance(networks, dict) or not networks:
        return None
    rx = tx = 0
    measured = False
    for interface in networks.values():
        if not isinstance(interface, dict):
            continue
        for key, is_rx in (("rx_bytes", True), ("tx_bytes", False)):
            value = interface.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNTER:
                continue
            measured = True
            if is_rx:
                rx += value
            else:
                tx += value
    if not measured:
        return None
    return rx, tx


def container_net_io(
    container_id: str,
    environ: dict[str, str] | os._Environ[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, int] | None:
    """Kumulativa (rx, tx) i byte för containern, eller None.

    None betyder "inte mätt": äldre daemon, stoppad container, `--network host`
    (tom `networks`-map), timeout eller ett svar som inte går att läsa. Agenten
    skickar då `null` i stället för en nolla som ser ut som en mätning.
    """
    if not isinstance(container_id, str) or not CONTAINER_ID_RE.fullmatch(container_id):
        return None
    try:
        connection = _connection(environ, timeout)
    except (OSError, ValueError):
        return None
    if connection is None:
        return None
    target = (
        f"/{DEFAULT_API_VERSION}/containers/{container_id}"
        "/stats?stream=false&one-shot=true"
    )
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        try:
            connection.close()
        except (OSError, http.client.HTTPException):
            pass
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        return None
    stats = _last_document(raw)
    if not isinstance(stats, dict):
        return None
    return _sum_networks(stats.get("networks"))

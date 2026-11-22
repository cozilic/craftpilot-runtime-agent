"""Docker-fria tester för nätverksräknarna (Docker Engine API, stdlib)."""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

MODULE_PATH = Path(__file__).with_name("network_stats.py")
SPEC = importlib.util.spec_from_file_location("network_stats", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
network_stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network_stats
SPEC.loader.exec_module(network_stats)

CONTAINER_ID = "a" * 64


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self.body = body

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]


class FakeConnection:
    def __init__(self, response=None, fail: BaseException | None = None):
        self.response = response
        self.fail = fail
        self.requests: list[tuple[str, str]] = []
        self.closed = False

    def request(self, method: str, target: str) -> None:
        self.requests.append((method, target))

    def getresponse(self):
        if self.fail is not None:
            raise self.fail
        return self.response

    def close(self) -> None:
        self.closed = True


def stats_body(networks) -> bytes:
    return json.dumps({"read": "2026-11-18T14:09:13.842221932Z", "networks": networks}).encode()


class ContainerNetIoTests(unittest.TestCase):
    def _measure(self, body: bytes, status: int = 200, **connection_kwargs):
        connection = FakeConnection(FakeResponse(status, body), **connection_kwargs)
        with patch.object(network_stats, "_connection", return_value=connection):
            result = network_stats.container_net_io(CONTAINER_ID)
        return result, connection

    def test_two_interfaces_are_summed_into_one_rx_and_one_tx_value(self):
        result, connection = self._measure(stats_body({
            "eth0": {"rx_bytes": 21084946999, "tx_bytes": 933300257},
            "eth5": {"rx_bytes": 4641, "tx_bytes": 690},
        }))

        self.assertEqual((21084951640, 933300947), result)
        self.assertEqual([("GET",
                           f"/v1.41/containers/{CONTAINER_ID}/stats?stream=false&one-shot=true")],
                         connection.requests)
        self.assertTrue(connection.closed)

    def test_missing_or_empty_networks_is_not_a_guess(self):
        for body in (json.dumps({"read": "x"}).encode(), json.dumps({"networks": {}}).encode(),
                     json.dumps({"networks": None}).encode(), b"{}"):
            with self.subTest(body=body):
                self.assertIsNone(self._measure(body)[0])

    def test_unknown_or_invalid_interface_values_are_skipped(self):
        result, _connection = self._measure(stats_body({
            "eth0": {"rx_bytes": True, "tx_bytes": "12"},
            "eth1": {"rx_bytes": -5, "tx_bytes": 2**64},
            "eth2": {"rx_bytes": 40},
            "loopback": "not-a-map",
        }))
        self.assertEqual((40, 0), result)

    def test_no_usable_value_returns_none_instead_of_zero(self):
        self.assertIsNone(self._measure(stats_body({"eth0": {"rx_packets": 3}}))[0])

    def test_error_status_timeout_and_garbage_all_return_none(self):
        for status, body, fail in ((500, b"", None), (404, b"", None), (200, b"not json", None),
                                   (200, b"", socket.timeout("stats timed out")),
                                   (200, b"", OSError("docker socket missing"))):
            with self.subTest(status=status, fail=fail):
                self.assertIsNone(self._measure(body, status=status, fail=fail)[0])

    def test_only_the_last_json_document_is_used(self):
        first = json.dumps({"networks": {"eth0": {"rx_bytes": 1, "tx_bytes": 1}}}).encode()
        second = stats_body({"eth0": {"rx_bytes": 900, "tx_bytes": 300}})
        result, _connection = self._measure(first + second)
        self.assertEqual((900, 300), result)

    def test_invalid_container_id_never_reaches_the_daemon(self):
        with patch.object(network_stats, "_connection") as connection:
            self.assertIsNone(network_stats.container_net_io("../etc/passwd"))
            self.assertIsNone(network_stats.container_net_io("a" * 64 + "\n"))
            self.assertIsNone(network_stats.container_net_io(""))
        connection.assert_not_called()

    def test_oversized_response_is_treated_as_unreadable(self):
        huge = b"{" + b" " * (network_stats.MAX_RESPONSE_BYTES + 10) + b"}"
        self.assertIsNone(self._measure(huge)[0])


class DockerEndpointTests(unittest.TestCase):
    def test_docker_host_selects_unix_tcp_or_nothing(self):
        cases = (
            ({}, ("unix", "/var/run/docker.sock")),
            ({"DOCKER_HOST": "unix:///run/docker.sock"}, ("unix", "/run/docker.sock")),
            ({"DOCKER_HOST": "tcp://10.0.0.5:2375"}, ("tcp", "10.0.0.5:2375")),
            ({"DOCKER_HOST": "http://10.0.0.5:2375/"}, ("tcp", "10.0.0.5:2375")),
            ({"DOCKER_HOST": "npipe:////./pipe/docker_engine"}, None),
            ({"DOCKER_HOST": "unix://"}, None),
            ({"DOCKER_HOST": "garbage"}, None),
        )
        for environ, expected in cases:
            with self.subTest(environ=environ):
                self.assertEqual(expected, network_stats.docker_endpoint(environ))

    def test_unsupported_endpoint_returns_none_without_opening_a_socket(self):
        with patch.object(network_stats, "_UnixHTTPConnection") as unix:
            self.assertIsNone(network_stats.container_net_io(
                CONTAINER_ID, environ={"DOCKER_HOST": "npipe:////./pipe/docker_engine"}))
        unix.assert_not_called()


class UnixConnectionTests(unittest.TestCase):
    def test_unix_connection_connects_to_the_configured_socket(self):
        connection = network_stats._UnixHTTPConnection("/tmp/docker.sock", timeout=1.5)
        fake_socket = MagicMock()
        fake_module = SimpleNamespace(AF_UNIX=1, SOCK_STREAM=2, socket=MagicMock(return_value=fake_socket))
        with patch.object(network_stats, "socket", fake_module):
            connection.connect()

        fake_module.socket.assert_called_once_with(1, 2)
        fake_socket.settimeout.assert_called_once_with(1.5)
        fake_socket.connect.assert_called_once_with("/tmp/docker.sock")
        self.assertIs(fake_socket, connection.sock)

    def test_a_platform_without_unix_sockets_fails_softly(self):
        connection = network_stats._UnixHTTPConnection("/var/run/docker.sock")
        with patch.object(network_stats, "socket", SimpleNamespace(SOCK_STREAM=2)):
            with self.assertRaises(OSError):
                connection.connect()


if __name__ == "__main__":
    unittest.main()

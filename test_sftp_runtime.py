"""Tester för nodens SFTPGo-drift (Docker-fria, som resten av sviten)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("craftpilot_runtime_agent.py")
SPEC = importlib.util.spec_from_file_location("craftpilot_runtime_agent", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
agent_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent_module
SPEC.loader.exec_module(agent_module)

sftp = agent_module.sftp_runtime
RuntimeNodeAgent = agent_module.RuntimeNodeAgent
AgentError = agent_module.AgentError

SERVER_ID = "e7b4b542-70b0-4cf7-a2ac-2a6597de165d"
NODE_ID = "c4f6babc-0d57-4ab7-a71f-a6358498335a"
FINGERPRINT = "SHA256:" + "A" * 43
GOOD_PASSWORD = "CorrectHorseBatteryStaple"


def make_state(users: list[dict[str, object]] | None = None, revision: str = "rev-1") -> dict[str, object]:
    return {"revision": revision, "users": users if users is not None else [make_user()]}


def make_user(username: str = "u42", keys: list[str] | None = None,
              folders: list[dict[str, str]] | None = None, enabled: bool = True,
              password_set: bool = False, password_revision: int = 0) -> dict[str, object]:
    return {
        "username": username,
        "enabled": enabled,
        "public_keys": keys if keys is not None else ["ssh-ed25519 " + "A" * 60 + " user@example"],
        "password_set": password_set,
        "password_revision": password_revision,
        "folders": folders if folders is not None else [{"name": "myserver", "server_id": SERVER_ID}],
    }


def server_path(server_id: str) -> str:
    return f"/srv/craftpilot/servers/{server_id}"


class FakeDocker:
    """Minimal docker-CLI som svarar som en frisk nod."""

    def __init__(self, running: bool | None = None, network_missing: bool = False,
                 attached: bool = False):
        self.running = running
        self.network_missing = network_missing
        self.attached = attached
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["docker", "network", "inspect"]:
            return self._result(1 if self.network_missing else 0, "" if self.network_missing else "[]")
        if args[:3] == ["docker", "network", "create"]:
            self.network_missing = False
            return self._result(0, "")
        if args[:3] == ["docker", "network", "connect"]:
            self.attached = True
            return self._result(0, "")
        if args[:3] == ["docker", "inspect", "--format"]:
            template = args[3]
            if "State.Running" in template:
                if self.running is None:
                    return self._result(1, "", "No such object")
                return self._result(0, "true" if self.running else "false")
            if "NetworkSettings.Networks" in template:
                return self._result(0, "attached" if self.attached else "")
        if args[:2] == ["docker", "run"]:
            self.running = True
            return self._result(0, "container-id")
        if args[:2] == ["docker", "start"]:
            if self.running is None:
                return self._result(1, "", "No such container")
            self.running = True
            return self._result(0, "craftpilot-sftp")
        raise AssertionError(f"oväntat docker-anrop: {args}")

    @staticmethod
    def _result(code: int, stdout: str, stderr: str = ""):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class FakeClient:
    def __init__(self, payload: dict[str, object] | None = None, users: list[dict[str, object]] | None = None,
                 fail_status: bool = False):
        self.payload = payload or {"ssh": {"host_keys": [
            {"fingerprint": FINGERPRINT, "algorithms": ["ssh-ed25519"]},
        ]}}
        self.users = list(users or [])
        self.fail_status = fail_status
        self.calls: list[tuple] = []

    def status(self):
        if self.fail_status:
            raise sftp.SftpError("SFTPGo svarade inte")
        return self.payload

    def list_users(self):
        return list(self.users)

    def ensure_folder(self, name: str, mapped_path: str):
        self.calls.append(("folder", name, mapped_path))

    def put_user(self, payload: dict[str, object]):
        self.calls.append(("user", payload))

    def delete_user(self, username: str):
        self.calls.append(("delete", username))


def make_node(root: Path, runner: FakeDocker | None = None, client: FakeClient | None = None,
              docker_data_root: str | None = "/srv/craftpilot/servers") -> sftp.SftpNode:
    settings = sftp.SftpSettings(
        state_dir=root / "state",
        data_root=root / "data",
        docker_data_root=docker_data_root,
        port=2222,
    )
    runner = runner or FakeDocker(running=True, attached=True)
    client = client or FakeClient()
    return sftp.SftpNode(
        settings,
        runner=runner,
        client_factory=lambda *_args: client,
        docker_available=lambda: True,
    )


class SftpContainerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_a_missing_container_is_created_with_pinned_image_and_host_data_mount(self):
        runner = FakeDocker(running=None, network_missing=True, attached=False)
        node = make_node(self.root, runner)

        self.assertEqual(FINGERPRINT, node.ensure_ready())

        run_args = next(call for call in runner.calls if call[:2] == ["docker", "run"])
        joined = " ".join(run_args)
        self.assertIn(sftp.DEFAULT_IMAGE, run_args)
        self.assertIn("--network craftpilot-sftp", joined)
        self.assertIn("-p 2222:2022", joined)
        self.assertIn("-v /srv/craftpilot/servers:/srv/craftpilot/servers", joined)
        self.assertIn("-v craftpilot-sftp-data:/var/lib/sftpgo", joined)
        self.assertIn("-v craftpilot-sftp-home:/sftp-home", joined)
        self.assertIn("--user 0:0", joined)

        password = (self.root / "state" / sftp.ADMIN_PASSWORD_FILE).read_text(encoding="utf-8").strip()
        self.assertIn(f"SFTPGO_DEFAULT_ADMIN_PASSWORD={password}", joined)
        self.assertEqual(1, joined.count(password))

    def test_an_existing_stopped_container_is_started_and_the_agent_joins_its_network(self):
        runner = FakeDocker(running=False, attached=False)
        node = make_node(self.root, runner)

        self.assertEqual(FINGERPRINT, node.ensure_ready())

        self.assertIn(["docker", "start", "craftpilot-sftp"], runner.calls)
        self.assertIn(["docker", "network", "connect", "craftpilot-sftp", "craftpilot-runtime-agent"],
                      runner.calls)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in runner.calls))

    def test_a_healthy_node_is_only_inspected_not_restarted(self):
        runner = FakeDocker(running=True, attached=True)
        node = make_node(self.root, runner)

        node.ensure_ready()
        runner.calls.clear()
        self.assertEqual(FINGERPRINT, node.ensure_ready())
        self.assertTrue(all("inspect" in call for call in runner.calls))
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "start"]) for call in runner.calls))

    def test_unhealthy_sftpgo_reports_no_fingerprint(self):
        node = make_node(self.root, client=FakeClient(fail_status=True))
        self.assertIsNone(node.ensure_ready())

    def test_a_node_without_a_docker_data_root_never_starts_sftpgo(self):
        runner = FakeDocker(running=None)
        node = make_node(self.root, runner, docker_data_root=None)

        self.assertIsNone(node.ensure_ready())
        self.assertEqual([], runner.calls)

    def test_ed25519_wins_when_sftpgo_has_several_host_keys(self):
        payload = {"ssh": {"host_keys": [
            {"fingerprint": "SHA256:" + "B" * 43, "algorithms": ["rsa-sha2-512", "ssh-rsa"]},
            {"fingerprint": FINGERPRINT, "algorithms": ["ssh-ed25519"]},
        ]}}
        self.assertEqual(FINGERPRINT, sftp.host_key_fingerprint(payload))

    def test_an_invalid_host_key_is_not_reported(self):
        payload = {"ssh": {"host_keys": [{"fingerprint": "SHA256:not-a-key", "algorithms": ["ssh-ed25519"]}]}}
        self.assertIsNone(sftp.host_key_fingerprint(payload))
        self.assertIsNone(sftp.host_key_fingerprint({"ssh": {}}))
        self.assertIsNone(sftp.host_key_fingerprint(None))

    @unittest.skipIf(os.name == "nt", "POSIX-behörigheter kontrolleras bara på Linux/macOS")
    def test_the_admin_password_is_created_once_with_owner_only_permissions(self):
        node = make_node(self.root)
        node.ensure_ready()
        path = self.root / "state" / sftp.ADMIN_PASSWORD_FILE
        first = path.read_text(encoding="utf-8").strip()
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

        second = make_node(self.root)
        second.ensure_ready()
        self.assertEqual(first, path.read_text(encoding="utf-8").strip())

    def test_a_corrupt_admin_password_file_is_refused_instead_of_rotated(self):
        state = self.root / "state"
        state.mkdir(mode=0o700, parents=True)
        path = state / sftp.ADMIN_PASSWORD_FILE
        path.write_text("short\n", encoding="utf-8")

        node = make_node(self.root)
        with self.assertLogs("craftpilot.runtime_agent", level="WARNING"):
            self.assertIsNone(node.ensure_ready())
        self.assertEqual("short\n", path.read_text(encoding="utf-8"))


class SftpStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = FakeClient()
        self.node = make_node(self.root, client=self.client)

    def test_folders_are_created_before_users(self):
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)

        self.assertEqual([
            ("folder", "myserver", server_path(SERVER_ID)),
            ("user", self.client.calls[1][1]),
        ], self.client.calls)

    def test_the_user_payload_is_the_full_master_contract(self):
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)
        payload = self.client.calls[1][1]

        self.assertEqual(1, payload["status"])
        self.assertEqual("u42", payload["username"])
        self.assertEqual("/sftp-home/u42", payload["home_dir"])
        self.assertEqual(33, payload["uid"])
        self.assertEqual(33, payload["gid"])
        self.assertEqual({"/": ["list"], "/myserver": ["*"]}, payload["permissions"])
        self.assertEqual([{"name": "myserver", "virtual_path": "/myserver"}], payload["virtual_folders"])
        self.assertEqual(["ssh-ed25519 " + "A" * 60 + " user@example"], payload["public_keys"])
        self.assertNotIn("password", payload)

    def test_a_user_without_keys_is_not_created_and_loses_an_existing_account(self):
        self.client.users = [{"username": "u42"}]
        self.node.apply_state(make_state([make_user(keys=[])]), {SERVER_ID}, server_path)

        self.assertFalse(any(call[0] == "user" for call in self.client.calls))
        self.assertIn(("delete", "u42"), self.client.calls)

    def test_a_disabled_user_is_created_inactive(self):
        self.node.apply_state(make_state([make_user(enabled=False)]), {SERVER_ID}, server_path)
        payload = self.client.calls[1][1]
        self.assertEqual(0, payload["status"])

    def test_unknown_accounts_are_removed_but_foreign_accounts_are_left_alone(self):
        self.client.users = [{"username": "u42"}, {"username": "u99"}, {"username": "admin"}]
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)

        deletes = [call for call in self.client.calls if call[0] == "delete"]
        self.assertEqual([("delete", "u99")], deletes)

    def test_a_server_that_is_not_assigned_to_the_node_is_rejected(self):
        with self.assertRaises(sftp.SftpError):
            self.node.apply_state(make_state(), set(), server_path)
        self.assertEqual([], self.client.calls)

    def test_a_foreign_username_is_rejected(self):
        with self.assertRaises(sftp.SftpError):
            self.node.apply_state(make_state([make_user(username="alice")]), {SERVER_ID}, server_path)

    def test_an_unsafe_folder_name_is_rejected(self):
        folders = [{"name": "../escape", "server_id": SERVER_ID}]
        with self.assertRaises(sftp.SftpError):
            self.node.apply_state(make_state([make_user(folders=folders)]), {SERVER_ID}, server_path)

    def test_a_path_outside_the_docker_root_is_rejected(self):
        with self.assertRaises(sftp.SftpError):
            self.node.apply_state(make_state(), {SERVER_ID}, lambda _server_id: "/tmp/elsewhere")

    def test_an_unchanged_revision_is_not_reapplied(self):
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)
        self.client.calls.clear()
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)
        self.assertEqual([], self.client.calls)

    def test_a_new_revision_is_applied(self):
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)
        self.client.calls.clear()
        self.node.apply_state(make_state(revision="rev-2"), {SERVER_ID}, server_path)
        self.assertIn(("folder", "myserver", server_path(SERVER_ID)), self.client.calls)

    def test_a_restarted_sftpgo_forces_a_full_reapply(self):
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)
        self.client.calls.clear()
        self.node._container_restarted = True
        self.node.apply_state(make_state(), {SERVER_ID}, server_path)
        self.assertIn(("folder", "myserver", server_path(SERVER_ID)), self.client.calls)


class SftpPasswordTests(unittest.TestCase):
    """Lösenordsrevisionen, minnet och kvittensen mot SFTPGo."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = FakeClient()
        self.node = make_node(self.root, client=self.client)

    def test_only_a_newer_password_revision_needs_a_secret(self):
        state = make_state([make_user(password_set=True, password_revision=2)])

        self.assertEqual({"u42": 2}, self.node.password_requirements(state))
        self.node._password_revisions = {"u42": 2}
        self.assertEqual({}, self.node.password_requirements(state))

    def test_a_clear_never_requests_a_secret(self):
        state = make_state([make_user(password_set=False, password_revision=3)])
        self.node._password_revisions = {"u42": 2}
        self.assertEqual({}, self.node.password_requirements(state))

    def test_a_secret_is_applied_and_acknowledged_without_leaking_it(self):
        state = make_state([make_user(password_set=True, password_revision=1)])

        reports = self.node.apply_state(
            state, {SERVER_ID}, server_path,
            passwords={"u42": (1, GOOD_PASSWORD)},
        )

        payload = self.client.calls[1][1]
        self.assertEqual(GOOD_PASSWORD, payload["password"])
        self.assertEqual([{"username": "u42", "password_revision": 1}], reports)
        self.assertEqual({"u42": 1}, self.node._password_revisions)
        self.assertNotIn(GOOD_PASSWORD, json.dumps(reports))
        self.assertNotIn(GOOD_PASSWORD, json.dumps(self.node._last_status))
        self.assertNotIn(GOOD_PASSWORD, repr(self.node.__dict__))
        self.assertEqual({}, self.node.password_requirements(state))

    def test_a_missing_secret_leaves_the_key_account_without_a_password(self):
        state = make_state([make_user(password_set=True, password_revision=3)])

        reports = self.node.apply_state(state, {SERVER_ID}, server_path, passwords={})

        payload = self.client.calls[1][1]
        self.assertEqual("", payload["password"])
        self.assertEqual(["ssh-ed25519 " + "A" * 60 + " user@example"], payload["public_keys"])
        self.assertEqual([{"username": "u42", "password_revision": 0}], reports)

    def test_clear_removes_an_existing_password_and_acks_the_new_revision(self):
        self.node._password_revisions = {"u42": 1}
        state = make_state([make_user(password_set=False, password_revision=2)])

        reports = self.node.apply_state(state, {SERVER_ID}, server_path)

        payload = self.client.calls[1][1]
        self.assertEqual("", payload["password"])
        self.assertEqual([{"username": "u42", "password_revision": 2}], reports)
        self.assertEqual({"u42": 2}, self.node._password_revisions)

    def test_a_restarted_agent_still_clears_a_password_it_no_longer_knows(self):
        state = make_state([make_user(password_set=False, password_revision=4)])

        reports = self.node.apply_state(state, {SERVER_ID}, server_path)

        self.assertEqual("", self.client.calls[1][1]["password"])
        self.assertEqual([{"username": "u42", "password_revision": 4}], reports)

    def test_a_password_only_account_is_created(self):
        state = make_state([
            make_user(keys=[], password_set=True, password_revision=1),
        ])

        self.node.apply_state(
            state, {SERVER_ID}, server_path,
            passwords={"u42": (1, GOOD_PASSWORD)},
        )

        payload = self.client.calls[1][1]
        self.assertEqual([], payload["public_keys"])
        self.assertEqual(GOOD_PASSWORD, payload["password"])

    def test_a_keyless_user_without_a_secret_is_removed_not_created(self):
        self.client.users = [{"username": "u42"}]
        state = make_state([make_user(keys=[], password_set=True, password_revision=1)])

        reports = self.node.apply_state(state, {SERVER_ID}, server_path, passwords={})

        self.assertFalse(any(call[0] == "user" for call in self.client.calls))
        self.assertIn(("delete", "u42"), self.client.calls)
        self.assertEqual([{"username": "u42", "password_revision": 0}], reports)

    def test_a_new_revision_replaces_the_previous_password(self):
        self.node._password_revisions = {"u42": 1}
        state = make_state([make_user(password_set=True, password_revision=2)], revision="rev-2")

        self.node.apply_state(
            state, {SERVER_ID}, server_path,
            passwords={"u42": (2, GOOD_PASSWORD + "-new")},
        )

        self.assertEqual(GOOD_PASSWORD + "-new", self.client.calls[1][1]["password"])
        self.assertEqual({"u42": 2}, self.node._password_revisions)

    def test_an_incomplete_secret_fetch_is_not_cached(self):
        state = make_state([make_user(password_set=True, password_revision=1)])

        self.node.apply_state(
            state, {SERVER_ID}, server_path,
            passwords={}, passwords_complete=False,
        )

        self.assertIsNone(self.node._last_revision)
        self.assertEqual({"u42": 1}, self.node.password_requirements(state))
        self.assertEqual("", self.client.calls[1][1]["password"])

    def test_a_transient_secret_failure_does_not_clear_a_working_password(self):
        self.node._password_revisions = {"u42": 1}
        state = make_state(
            [make_user(password_set=True, password_revision=2)], revision="rev-2",
        )

        reports = self.node.apply_state(
            state, {SERVER_ID}, server_path,
            passwords={}, passwords_complete=False,
        )

        self.assertNotIn("password", self.client.calls[1][1])
        self.assertEqual([{"username": "u42", "password_revision": 1}], reports)
        self.assertEqual({"u42": 1}, self.node._password_revisions)
        self.assertEqual({"u42": 2}, self.node.password_requirements(state))

    def test_a_cached_state_returns_the_status_for_a_retry(self):
        state = make_state([make_user(password_set=True, password_revision=1)])
        first = self.node.apply_state(
            state, {SERVER_ID}, server_path,
            passwords={"u42": (1, GOOD_PASSWORD)},
        )
        self.client.calls.clear()

        second = self.node.apply_state(state, {SERVER_ID}, server_path)

        self.assertEqual(first, second)
        self.assertEqual([], self.client.calls)

    def test_status_reports_are_capped_at_the_master_limit(self):
        users = [
            make_user(username=f"u{index}", password_set=True, password_revision=1)
            for index in range(1, 502)
        ]
        state = make_state(users)
        passwords = {f"u{index}": (1, GOOD_PASSWORD) for index in range(1, 502)}

        reports = self.node.apply_state(
            state, {SERVER_ID}, server_path, passwords=passwords,
        )

        self.assertEqual(sftp.MAX_STATUS_USERS, len(reports))
        self.assertTrue(all(report["password_revision"] == 1 for report in reports))


class SftpPasswordSecretParsingTests(unittest.TestCase):
    def test_a_matching_secret_is_returned(self):
        payload = {"secrets": [{"username": "u42", "password": GOOD_PASSWORD, "revision": 3}]}
        self.assertEqual(
            {"u42": (3, GOOD_PASSWORD)},
            sftp.parse_password_secrets(payload, {"u42": 3}),
        )

    def test_other_users_and_stale_revisions_are_ignored(self):
        payload = {"secrets": [
            {"username": "u99", "password": GOOD_PASSWORD, "revision": 3},
            {"username": "u42", "password": GOOD_PASSWORD, "revision": 2},
        ]}
        self.assertEqual({}, sftp.parse_password_secrets(payload, {"u42": 3}))

    def test_malformed_secrets_are_rejected(self):
        cases = [
            None,
            {},
            {"secrets": [None]},
            {"secrets": [{"username": "alice", "password": GOOD_PASSWORD, "revision": 1}]},
            {"secrets": [{"username": "u42", "password": "short", "revision": 1}]},
            {"secrets": [{"username": "u42", "password": GOOD_PASSWORD, "revision": True}]},
            {"secrets": [
                {"username": "u42", "password": GOOD_PASSWORD, "revision": 1},
                {"username": "u42", "password": GOOD_PASSWORD, "revision": 1},
            ]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(sftp.SftpError):
                    sftp.parse_password_secrets(payload, {"u42": 1})


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, url: str, code: int):
        super().__init__(url, code, "error", {}, None)

    def close(self):  # pragma: no cover - bara för att matcha urllib
        pass


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.requests: list[object] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, int):
            raise FakeHTTPError(request.full_url, item)
        return FakeResponse(item)


class SftpGoClientTests(unittest.TestCase):
    def test_the_client_authenticates_and_lists_users(self):
        opener = FakeOpener([
            {"access_token": "t" * 20},
            [{"username": "u42"}],
        ])
        client = sftp.SftpGoClient("http://sftp:8080", "admin", "p" * 40, opener=opener)

        self.assertEqual([{"username": "u42"}], client.list_users())
        self.assertTrue(opener.requests[0].full_url.endswith("/api/v2/token"))
        self.assertEqual("Bearer " + "t" * 20,
                         opener.requests[1].get_header("Authorization"))

    def test_a_missing_user_is_accepted_as_already_removed(self):
        opener = FakeOpener([{"access_token": "t" * 20}, 404])
        client = sftp.SftpGoClient("http://sftp:8080", "admin", "p" * 40, opener=opener)
        client.delete_user("u42")

    def test_an_expired_token_is_refreshed_once(self):
        opener = FakeOpener([
            {"access_token": "t" * 20},
            401,
            {"access_token": "n" * 20},
            {"ssh": {}},
        ])
        client = sftp.SftpGoClient("http://sftp:8080", "admin", "p" * 40, opener=opener)

        self.assertEqual({"ssh": {}}, client.status())
        self.assertEqual("Bearer " + "n" * 20,
                         opener.requests[-1].get_header("Authorization"))

    def test_a_failed_login_is_a_sftp_error(self):
        opener = FakeOpener([401])
        client = sftp.SftpGoClient("http://sftp:8080", "admin", "p" * 40, opener=opener)
        with self.assertRaises(sftp.SftpError):
            client.status()

    def test_a_204_response_is_accepted(self):
        opener = FakeOpener([{"access_token": "t" * 20}, b""])
        client = sftp.SftpGoClient("http://sftp:8080", "admin", "p" * 40, opener=opener)
        self.assertEqual({}, client._call("POST", "/api/v2/users"))


class MemoryStateStore:
    def __init__(self):
        self.saved: list[dict[str, object]] = []

    def save(self, state):
        self.saved.append(json.loads(json.dumps(state)))


class FakeSftp:
    def __init__(self, fingerprint: str | None = FINGERPRINT):
        self.fingerprint = fingerprint
        self.ready_calls = 0
        self.applied: list[tuple[dict[str, object], set[str]]] = []
        self.requirements: dict[str, int] = {}
        self.passwords: dict[str, tuple[int, str]] = {}
        self.passwords_complete = True
        self.reports: list[dict[str, object]] = []
        self.fail_apply = False

    def ensure_ready(self):
        self.ready_calls += 1
        return self.fingerprint

    def password_requirements(self, _state):
        return dict(self.requirements)

    def apply_state(self, state, assigned, resolve, passwords=None, passwords_complete=True):
        if self.fail_apply:
            raise sftp.SftpError("apply failed")
        resolve(SERVER_ID)
        self.applied.append((state, set(assigned)))
        self.passwords = dict(passwords or {})
        self.passwords_complete = passwords_complete
        return list(self.reports)


def make_agent_config(root: Path, **overrides):
    values = dict(
        master_url="https://master.example",
        node_id=NODE_ID,
        registration_token=None,
        state_dir=root / "state",
        data_root=root / "data",
        images=frozenset({"eclipse-temurin:21-jre"}),
        docker_data_root="/srv/craftpilot/servers",
        sftp_enabled=True,
    )
    values.update(overrides)
    return agent_module.Config(**values)


class SftpClaimTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = make_agent_config(self.root)
        self.sftp = FakeSftp()
        self.agent = RuntimeNodeAgent(
            self.config,
            runtime=SimpleNamespace(config=self.config, processes=SimpleNamespace(server_ids=lambda: []),
                                    _docker_server_data_path=server_path),
            store=MemoryStateStore(),
            sftp=self.sftp,
        )
        self.agent.state = {"node_id": NODE_ID, "credential": "c" * 64, "sequence": 0}
        self.agent._assigned_server_ids = lambda: [SERVER_ID]
        self.requests: list[tuple[str, dict[str, object] | None]] = []
        self.credential_requests: list[tuple[str, dict[str, object] | None]] = []
        self.secrets_response: dict[str, object] = {"secrets": [
            {"username": "u42", "password": GOOD_PASSWORD, "revision": 1},
        ]}
        self.fail_secrets = False
        self.fail_status = False

    def _request(self, method, path, body=None, credential=False):
        self.requests.append((path, body))
        if credential:
            self.credential_requests.append((path, body))
        if path.endswith("/sftp/secrets"):
            if self.fail_secrets:
                raise agent_module.AgentError("secrets unavailable")
            return self.secrets_response
        if path.endswith("/sftp/status"):
            if self.fail_status:
                raise agent_module.AgentError("status unavailable")
            return {}
        if path.endswith("/sftp"):
            return make_state()
        if path.endswith("/commands/claim"):
            return {"command": None, "node_settings": {
                "name": "Node", "max_servers": 4, "poll_interval_seconds": 11,
            }}
        return {"ok": True}

    def test_the_claim_carries_features_and_host_key_after_a_successful_sync(self):
        self.agent._request = self._request

        self.agent._sync_sftp()
        self.agent._claim_and_execute()

        claim_body = self.requests[-1][1]
        self.assertEqual(["sftp", "sftp-password"], claim_body["features"])
        self.assertEqual(FINGERPRINT, claim_body["sftp_host_key_fingerprint"])
        self.assertEqual(1, len(self.sftp.applied))

    def test_an_unhealthy_node_reports_no_sftp_features(self):
        self.sftp.fingerprint = None
        self.agent._request = self._request

        self.agent._sync_sftp()
        self.agent._claim_and_execute()

        claim_body = self.requests[-1][1]
        self.assertNotIn("features", claim_body)
        self.assertNotIn("sftp_host_key_fingerprint", claim_body)

    def test_a_failed_state_apply_keeps_the_agent_polling_without_sftp(self):
        self.sftp.fail_apply = True
        self.agent._request = self._request

        with self.assertLogs("craftpilot.runtime_agent", level="WARNING") as captured:
            self.agent._sync_sftp()

        self.assertIsNone(self.agent._sftp_features)
        self.assertTrue(any("sftp_sync_failed" in line for line in captured.output))

    def test_a_rejected_credential_propagates_to_re_enrollment(self):
        def rejected(_method, _path, _body=None, credential=False):
            raise agent_module.CredentialRejectedError(401)

        self.agent._request = rejected
        with self.assertRaises(agent_module.CredentialRejectedError):
            self.agent._sync_sftp()

    def test_a_symlinked_server_directory_is_refused(self):
        data = self.config.data_root
        data.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (data / SERVER_ID).symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this host")

        with self.assertRaises(AgentError):
            self.agent._sftp_server_path(SERVER_ID)

    def test_a_process_node_never_syncs_and_sends_no_sftp_fields(self):
        agent = RuntimeNodeAgent(
            make_agent_config(self.root, sftp_enabled=False, docker_data_root=None),
            runtime=SimpleNamespace(config=self.config, processes=SimpleNamespace(server_ids=lambda: []),
                                    _docker_server_data_path=server_path),
            store=MemoryStateStore(),
        )
        agent.state = {"node_id": NODE_ID, "credential": "c" * 64, "sequence": 0}
        agent._request = self._request

        agent._sync_sftp()
        agent._claim_and_execute()

        claim_body = self.requests[-1][1]
        self.assertEqual({"sequence": 1, "agent_version": agent_module.AGENT_VERSION}, claim_body)

    def test_console_ticks_do_not_run_the_sftp_sync(self):
        self.agent._request = self._request
        self.agent._last_reports = {SERVER_ID: {"status": "ONLINE"}}
        self.agent._report_log_lines = lambda _sequence: None

        self.agent._console_tick()

        self.assertEqual(0, self.sftp.ready_calls)

    def test_the_password_channel_fetches_applies_and_acknowledges_204(self):
        self.sftp.requirements = {"u42": 1}
        self.sftp.reports = [{"username": "u42", "password_revision": 1}]
        self.agent._request = self._request

        self.agent._sync_sftp()

        paths = [path for path, _body in self.requests]
        self.assertIn(f"/api/runtime/nodes/{NODE_ID}/sftp/secrets", paths)
        self.assertIn(f"/api/runtime/nodes/{NODE_ID}/sftp/status", paths)
        self.assertEqual({"u42": (1, GOOD_PASSWORD)}, self.sftp.passwords)
        self.assertTrue(self.sftp.passwords_complete)
        status_body = next(body for path, body in self.requests if path.endswith("/sftp/status"))
        self.assertEqual({"users": [{"username": "u42", "password_revision": 1}]}, status_body)
        self.assertIn(
            f"/api/runtime/nodes/{NODE_ID}/sftp",
            [path for path, _body in self.credential_requests],
        )
        self.assertTrue(all(path.startswith("/api/runtime/nodes/") for path, _ in self.credential_requests))
        self.assertEqual(["sftp", "sftp-password"], self.agent._sftp_features)

    def test_no_password_requirement_means_no_secret_or_status_request(self):
        self.agent._request = self._request

        self.agent._sync_sftp()

        paths = [path for path, _body in self.requests]
        self.assertFalse(any(path.endswith("/sftp/secrets") for path in paths))
        self.assertFalse(any(path.endswith("/sftp/status") for path in paths))
        self.assertTrue(self.sftp.passwords_complete)

    def test_a_missing_secret_keeps_keys_and_reports_zero(self):
        self.sftp.requirements = {"u42": 1}
        self.sftp.reports = [{"username": "u42", "password_revision": 0}]
        self.secrets_response = {"secrets": []}
        self.agent._request = self._request

        self.agent._sync_sftp()

        self.assertEqual({}, self.sftp.passwords)
        self.assertTrue(self.sftp.passwords_complete)
        status_body = next(body for path, body in self.requests if path.endswith("/sftp/status"))
        self.assertEqual({"users": [{"username": "u42", "password_revision": 0}]}, status_body)

    def test_a_secrets_failure_keeps_sftp_and_marks_passwords_incomplete(self):
        self.sftp.requirements = {"u42": 1}
        self.sftp.reports = [{"username": "u42", "password_revision": 1}]
        self.fail_secrets = True
        self.agent._request = self._request

        with self.assertLogs("craftpilot.runtime_agent", level="WARNING") as captured:
            self.agent._sync_sftp()

        self.assertFalse(self.sftp.passwords_complete)
        self.assertEqual(["sftp", "sftp-password"], self.agent._sftp_features)
        self.assertIn("sftp_secrets_failed", "\n".join(captured.output))

    def test_a_secrets_failure_is_retried_after_backoff_not_on_every_poll(self):
        self.sftp.requirements = {"u42": 1}
        self.fail_secrets = True
        self.agent._request = self._request

        with self.assertLogs("craftpilot.runtime_agent", level="WARNING"):
            self.agent._sync_sftp()
        first = sum(1 for path, _ in self.requests if path.endswith("/sftp/secrets"))
        self.agent._sync_sftp()
        second = sum(1 for path, _ in self.requests if path.endswith("/sftp/secrets"))

        self.assertEqual(1, first)
        self.assertEqual(1, second)

    def test_a_status_failure_keeps_sftp_features_for_the_next_retry(self):
        self.sftp.requirements = {"u42": 1}
        self.sftp.reports = [{"username": "u42", "password_revision": 1}]
        self.fail_status = True
        self.agent._request = self._request

        with self.assertLogs("craftpilot.runtime_agent", level="WARNING") as captured:
            self.agent._sync_sftp()

        self.assertEqual(["sftp", "sftp-password"], self.agent._sftp_features)
        self.assertIn("sftp_status_failed", "\n".join(captured.output))

    def test_a_204_status_response_is_accepted_without_json(self):
        self.agent._opener = FakeOpener([b""])
        self.assertEqual({}, self.agent._request(
            "POST", f"/api/runtime/nodes/{NODE_ID}/sftp/status", {"users": []},
            credential=True,
        ))

    def test_the_password_never_reaches_state_claim_or_logs(self):
        self.sftp.requirements = {"u42": 1}
        self.sftp.reports = [{"username": "u42", "password_revision": 1}]
        self.agent._request = self._request

        self.agent._sync_sftp()
        self.agent._claim_and_execute()

        self.assertNotIn(GOOD_PASSWORD, json.dumps(self.agent.store.saved))
        claim_body = self.requests[-1][1]
        self.assertNotIn(GOOD_PASSWORD, json.dumps(claim_body))


class SftpConfigTests(unittest.TestCase):
    def test_sftp_is_disabled_by_default(self):
        with patch.dict(os.environ, {"CRAFTPILOT_MASTER_URL": "https://master.example"}, clear=True):
            config = agent_module.Config.from_env()
        self.assertFalse(config.sftp_enabled)
        self.assertEqual(sftp.DEFAULT_IMAGE, config.sftp_image)
        self.assertEqual(2222, config.sftp_port)

    def test_sftp_settings_are_read_from_the_environment(self):
        env = {
            "CRAFTPILOT_MASTER_URL": "https://master.example",
            "CRAFTPILOT_SFTP_ENABLED": "true",
            "CRAFTPILOT_SFTP_PORT": "2223",
            "CRAFTPILOT_SFTP_IMAGE": "drakkan/sftpgo:v2.6.4",
            "CRAFTPILOT_SFTP_NETWORK": "craftpilot-sftp",
            "CRAFTPILOT_SFTP_ADMIN_URL": "http://craftpilot-sftp:8080/",
        }
        with patch.dict(os.environ, env, clear=True):
            config = agent_module.Config.from_env()
        self.assertTrue(config.sftp_enabled)
        self.assertEqual(2223, config.sftp_port)
        self.assertEqual("http://craftpilot-sftp:8080", config.sftp_admin_url)

    def test_invalid_sftp_settings_are_rejected(self):
        base = {"CRAFTPILOT_MASTER_URL": "https://master.example"}
        for key, value in (
            ("CRAFTPILOT_SFTP_ENABLED", "maybe"),
            ("CRAFTPILOT_SFTP_PORT", "70000"),
            ("CRAFTPILOT_SFTP_IMAGE", "bad image"),
            ("CRAFTPILOT_SFTP_NETWORK", "bad network!"),
            ("CRAFTPILOT_SFTP_ADMIN_URL", "ftp://sftp:8080"),
            ("CRAFTPILOT_SFTP_ADMIN_USERNAME", "bad user"),
            ("CRAFTPILOT_SFTP_HOME", "relative/path"),
        ):
            with self.subTest(key=key):
                with patch.dict(os.environ, {**base, key: value}, clear=True):
                    with self.assertRaises(AgentError):
                        agent_module.Config.from_env()


if __name__ == "__main__":
    unittest.main()

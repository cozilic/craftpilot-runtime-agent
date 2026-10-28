"""Persistent, event-maintained directory listings for remote server files."""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import re
import select
import stat
import struct
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("craftpilot.runtime_agent")
INDEX_VERSION = 1
INDEX_FILENAME = "file-tree-index.json"
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 1001
MAX_DEPTH = 32

# inotify masks from <sys/inotify.h>.
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
WATCH_MASK = (IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
              | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF)
EVENT_HEADER = struct.Struct("iIII")


class FileTreeIndex:
    """Caches only directory metadata; file reads still go through the safe file API."""

    def __init__(self, data_root: Path, state_dir: Path):
        self.data_root = data_root
        self.state_dir = state_dir
        self.index_path = state_dir / INDEX_FILENAME
        self._lock = threading.RLock()
        self._directories: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._sync_updates: dict[str, tuple[int, dict[str, Any] | None]] = {}
        self._sync_revision = 0
        self._ready = False
        self._progress = "fetching"
        self._progress_callback: Callable[[str], None] | None = None
        self._needs_persist = False
        self._last_persist = 0.0
        self._started = False
        self._stop = threading.Event()
        self._fd: int | None = None
        self._libc: Any = None
        self._watch_paths: dict[int, str] = {}
        self._watches_by_path: dict[str, int] = {}
        self._root_fd: int | None = None
        self._load()

    @staticmethod
    def directory_key(server_id: str, relative: str) -> str:
        return server_id if not relative else f"{server_id}/{relative}"

    def start(self) -> None:
        """Start one background reconciliation and inotify watcher, if on Linux."""
        with self._lock:
            if self._started:
                return
            self._started = True
        if not sys.platform.startswith("linux"):
            return
        thread = threading.Thread(target=self._watch, name="craftpilot-file-tree", daemon=True)
        thread.start()

    def close(self) -> None:
        self._stop.set()
        fd = self._fd
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = None
        root_fd = self._root_fd
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass
            self._root_fd = None

    @property
    def progress(self) -> str:
        with self._lock:
            return self._progress

    def set_progress_callback(self, callback: Callable[[str], None] | None) -> None:
        with self._lock:
            self._progress_callback = callback

    def set_progress(self, stage: str, *, force: bool = False) -> None:
        if stage not in {"fetching", "building", "indexing", "ready"}:
            raise ValueError("invalid file tree progress stage")
        with self._lock:
            if self._progress == stage and not force:
                return
            self._progress = stage
            callback = self._progress_callback
        if callback is not None:
            callback(stage)

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._ready:
                    return True
            time.sleep(0.01)
        with self._lock:
            return self._ready

    def list_entries(
        self,
        server_id: str,
        relative: str,
        mtime_ns: int,
        scan: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        self.start()
        key = self.directory_key(server_id, relative)
        with self._lock:
            cached = self._directories.get(key)
            if (self._ready and key not in self._dirty and cached is not None
                    and cached.get("mtime_ns") == mtime_ns):
                return [dict(entry) for entry in cached["entries"]]

        entries = scan()
        self._store_directory(key, mtime_ns, entries)
        return entries

    def invalidate(self, server_id: str, relative: str) -> None:
        """Invalidate one directory after an agent-mediated file mutation."""
        key = self.directory_key(server_id, relative)
        with self._lock:
            self._dirty.add(key)
            self._needs_persist = True

    def pending_sync_updates(self, server_ids: set[str], limit: int = 50) -> list[tuple[str, int, dict[str, Any]]]:
        """Return fresh bounded listing deltas, prioritizing each assigned server's root."""
        with self._lock:
            if not self._ready or limit <= 0:
                return []
            dirty_keys = [key for key in self._dirty if key.split("/", 1)[0] in server_ids]
        for key in dirty_keys:
            self._refresh_directory(key)

        updates: list[tuple[str, int, dict[str, Any]]] = []
        with self._lock:
            pending = [
                (key, revision, listing)
                for key, (revision, listing) in self._sync_updates.items()
                if key.split("/", 1)[0] in server_ids
            ]
            pending.sort(key=lambda update: ("/" in update[0], update[0]))
            for key, revision, listing in pending[:limit]:
                server_id, separator, relative = key.partition("/")
                if listing is None:
                    update = {"server_id": server_id, "path": relative if separator else "", "deleted": True}
                else:
                    update = {"server_id": server_id, "path": relative if separator else "", "listing": listing}
                updates.append((key, revision, update))
        return updates

    def acknowledge_sync_updates(self, revisions: list[tuple[str, int]]) -> None:
        with self._lock:
            for key, revision in revisions:
                current = self._sync_updates.get(key)
                if current is not None and current[0] == revision:
                    self._sync_updates.pop(key, None)

    def _queue_sync_locked(self, key: str, listing: dict[str, Any] | None) -> None:
        self._sync_revision += 1
        self._sync_updates[key] = (self._sync_revision, listing)

    def _store_directory(self, key: str, mtime_ns: int, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            self._directories[key] = {"mtime_ns": mtime_ns, "entries": entries}
            self._dirty.discard(key)
            if self._ready and self._fd is not None:
                self._add_watch_for_key(key)
            if self._ready:
                normalized = key.partition("/")[2] if "/" in key else ""
                parent = normalized.rpartition("/")[0] if "/" in normalized else ("" if normalized else None)
                self._queue_sync_locked(key, {"path": normalized, "parent": parent, "entries": entries})
                self._persist_locked()
                self._needs_persist = False
                self._last_persist = time.monotonic()

    def _load(self) -> None:
        try:
            if self.index_path.stat().st_size > MAX_INDEX_BYTES:
                return
            document = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(document, dict) or document.get("version") != INDEX_VERSION:
            return
        directories = document.get("directories")
        if not isinstance(directories, dict):
            return
        loaded: dict[str, dict[str, Any]] = {}
        for key, record in directories.items():
            if not self._valid_key(key) or not isinstance(record, dict):
                return
            mtime_ns, entries = record.get("mtime_ns"), record.get("entries")
            if (isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int)
                    or not isinstance(entries, list) or len(entries) > MAX_DIRECTORY_ENTRIES
                    or any(not self._valid_entry(entry) for entry in entries)):
                return
            loaded[key] = {"mtime_ns": mtime_ns, "entries": entries}
        # Persisted data is never considered fresh until startup reconciliation
        # has completed; it remains useful for recovery diagnostics only.
        self._directories = loaded
        self._dirty = set(loaded)

    @staticmethod
    def _valid_key(key: Any) -> bool:
        if not isinstance(key, str) or not key or key.startswith("/") or "\\" in key:
            return False
        parts = key.split("/")
        if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", parts[0]):
            return False
        if any(part in ("", ".", "..") for part in parts):
            return False
        return len(parts) <= MAX_DEPTH + 1

    @staticmethod
    def _valid_entry(entry: Any) -> bool:
        if not isinstance(entry, dict) or set(entry) != {"name", "type", "size", "modified_at"}:
            return False
        name, kind, size, modified = entry["name"], entry["type"], entry["size"], entry["modified_at"]
        if (not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name
                or kind not in ("dir", "file", "link") or not isinstance(modified, str)):
            return False
        return size is None if kind == "dir" else isinstance(size, int) and not isinstance(size, bool) and size >= 0

    @staticmethod
    def scan_path(directory: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        with os.scandir(directory) as iterator:
            for item in iterator:
                if len(entries) >= MAX_DIRECTORY_ENTRIES:
                    break
                name = item.name
                if (not name or name in (".", "..") or len(os.fsencode(name)) > 255
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)
                        or "/" in name or "\\" in name):
                    continue
                info = item.stat(follow_symlinks=False)
                entries.append(FileTreeIndex._entry(name, info))
        return FileTreeIndex._sort_entries(entries)

    @staticmethod
    def scan_directory(directory_fd: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        with os.scandir(directory_fd) as iterator:
            for item in iterator:
                if len(entries) >= MAX_DIRECTORY_ENTRIES:
                    break
                name = item.name
                if (not name or name in (".", "..") or len(os.fsencode(name)) > 255
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)
                        or "/" in name or "\\" in name):
                    continue
                info = item.stat(follow_symlinks=False)
                entries.append(FileTreeIndex._entry(name, info))
        return FileTreeIndex._sort_entries(entries)

    @staticmethod
    def _entry(name: str, info: os.stat_result) -> dict[str, Any]:
        if stat.S_ISLNK(info.st_mode):
            kind, size = "link", info.st_size
        elif stat.S_ISDIR(info.st_mode):
            kind, size = "dir", None
        elif stat.S_ISREG(info.st_mode):
            kind, size = "file", info.st_size
        else:
            kind, size = "link", None
        modified = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        return {"name": name, "type": kind, "size": size, "modified_at": modified}

    @staticmethod
    def _sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rank = {"dir": 0, "link": 1, "file": 2}
        entries.sort(key=lambda item: (rank[item["type"]], item["name"]))
        return entries

    def _watch(self) -> None:
        if not hasattr(os, "O_CLOEXEC"):
            return
        libc = ctypes.CDLL(None, use_errno=True)
        init1 = getattr(libc, "inotify_init1", None)
        add_watch = getattr(libc, "inotify_add_watch", None)
        rm_watch = getattr(libc, "inotify_rm_watch", None)
        if init1 is None or add_watch is None or rm_watch is None:
            return
        init1.argtypes, init1.restype = [ctypes.c_int], ctypes.c_int
        add_watch.argtypes, add_watch.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
        rm_watch.argtypes, rm_watch.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int
        fd = init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return
        self._fd, self._libc = fd, libc
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            self._root_fd = os.open(self.data_root, flags)
            self._add_watch_fd(self._root_fd, "")
            self._rebuild_all()
        except OSError as exc:
            LOGGER.warning("file_tree_index_start_failed error=%s", str(exc)[:200])
            self.close()
            return

        LOGGER.info("file_tree_index_ready directories=%d", len(self._directories))
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], 0.25)
                if not readable:
                    continue
                events = self._read_events(fd)
                if any(mask & IN_Q_OVERFLOW for _wd, mask, _cookie, _name in events):
                    self._rebuild_all()
                    continue
                parents: set[str] = set()
                for wd, mask, _cookie, name in events:
                    if mask & IN_IGNORED:
                        self._forget_watch(wd)
                        continue
                    parent_key = self._watch_paths.get(wd)
                    if parent_key is None:
                        continue
                    if parent_key:
                        parents.add(parent_key)
                    if not name:
                        continue
                    child_key = f"{parent_key}/{name}" if parent_key else name
                    if not parent_key and not self._valid_key(child_key):
                        continue
                    if mask & IN_ISDIR:
                        if mask & (IN_DELETE | IN_MOVED_FROM):
                            self._prune_subtree(child_key)
                        if mask & (IN_CREATE | IN_MOVED_TO):
                            self._index_subtree(child_key)
                    if parent_key:
                        parents.add(parent_key)
                    elif len(name) == 36:
                        parents.add("")
                for key in parents:
                    if key:
                        self._refresh_directory(key)
                if events:
                    with self._lock:
                        self._needs_persist = True
                now = time.monotonic()
                if now - self._last_persist >= 0.5:
                    with self._lock:
                        if self._needs_persist:
                            self._persist_locked()
                            self._needs_persist = False
                            self._last_persist = now
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    LOGGER.warning("file_tree_index_watch_failed error=%s", str(exc)[:200])
                    time.sleep(0.25)

    @staticmethod
    def _read_events(fd: int) -> list[tuple[int, int, int, str]]:
        try:
            raw = os.read(fd, 1024 * 1024)
        except BlockingIOError:
            return []
        events: list[tuple[int, int, int, str]] = []
        offset = 0
        while offset + EVENT_HEADER.size <= len(raw):
            wd, mask, cookie, name_length = EVENT_HEADER.unpack_from(raw, offset)
            offset += EVENT_HEADER.size
            if offset + name_length > len(raw):
                break
            raw_name = raw[offset:offset + name_length].split(b"\0", 1)[0]
            offset += name_length
            events.append((wd, mask, cookie, os.fsdecode(raw_name)))
        return events

    def _add_watch_fd(self, directory_fd: int, key: str) -> None:
        if self._fd is None or self._libc is None:
            return
        # Watching the opened directory avoids following a symlink if a game
        # process replaces a path while the index is being reconciled.
        path = f"/proc/self/fd/{directory_fd}".encode()
        wd = self._libc.inotify_add_watch(self._fd, path, WATCH_MASK)
        if wd < 0:
            error = ctypes.get_errno()
            if error not in (errno.ENOENT, errno.ENOTDIR, errno.EACCES):
                raise OSError(error, os.strerror(error))
            return
        previous = self._watch_paths.get(wd)
        if previous is not None and previous != key:
            self._watches_by_path.pop(previous, None)
        self._watch_paths[wd] = key
        self._watches_by_path[key] = wd

    def _add_watch_for_key(self, key: str) -> None:
        if key in self._watches_by_path:
            return
        directory_fd = self._open_key(key)
        try:
            self._add_watch_fd(directory_fd, key)
        finally:
            os.close(directory_fd)

    def _forget_watch(self, wd: int) -> None:
        key = self._watch_paths.pop(wd, None)
        if key is not None:
            self._watches_by_path.pop(key, None)

    def _open_key(self, key: str) -> int:
        if self._root_fd is None:
            raise OSError(errno.ENOENT, "file tree root is not open")
        parts = key.split("/")
        if not self._valid_key(key):
            raise OSError(errno.EINVAL, "invalid indexed directory path")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current = os.dup(self._root_fd)
        try:
            for part in parts:
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
            return current
        except Exception:
            os.close(current)
            raise

    def _rebuild_all(self) -> None:
        if self._root_fd is None:
            return
        self.set_progress("fetching")
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        rebuilt: dict[str, dict[str, Any]] = {}
        server_directories: list[tuple[str, int]] = []
        with os.scandir(self._root_fd) as servers:
            for server in servers:
                if len(server.name) != 36 or server.is_symlink() or not server.is_dir(follow_symlinks=False):
                    continue
                key = server.name
                try:
                    server_fd = os.open(server.name, root_flags, dir_fd=self._root_fd)
                except OSError:
                    continue
                server_directories.append((key, server_fd))
        self.set_progress("building")
        for key, server_fd in server_directories:
            self._scan_subtree_fd(server_fd, key, rebuilt, depth=0)
        self.set_progress("indexing", force=True)
        with self._lock:
            previous_keys = set(self._directories)
            self._directories = rebuilt
            self._dirty.clear()
            for removed_key in previous_keys - set(rebuilt):
                self._queue_sync_locked(removed_key, None)
            for key, record in rebuilt.items():
                _server_id, separator, relative = key.partition("/")
                parent = relative.rpartition("/")[0] if "/" in relative else ("" if separator else None)
                self._queue_sync_locked(key, {
                    "path": relative if separator else "",
                    "parent": parent,
                    "entries": record["entries"],
                })
            self._ready = True
            self._persist_locked()
            self._needs_persist = False
            self._last_persist = time.monotonic()
        self.set_progress("ready")

    def _scan_subtree_fd(self, directory_fd: int, key: str, output: dict[str, dict[str, Any]], depth: int) -> None:
        try:
            info = os.fstat(directory_fd)
            entries = self.scan_directory(directory_fd)
            output[key] = {"mtime_ns": info.st_mtime_ns, "entries": entries}
            self._add_watch_fd(directory_fd, key)
            if depth >= MAX_DEPTH:
                return
            for entry in entries:
                if entry["type"] != "dir":
                    continue
                child_key = f"{key}/{entry['name']}"
                try:
                    child_fd = os.open(entry["name"], os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                       | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                except OSError:
                    continue
                self._scan_subtree_fd(child_fd, child_key, output, depth + 1)
        finally:
            os.close(directory_fd)

    def _index_subtree(self, key: str) -> None:
        if self._root_fd is None or not self._valid_key(key):
            return
        try:
            descriptor = self._open_key(key)
        except OSError:
            return
        indexed: dict[str, dict[str, Any]] = {}
        self._scan_subtree_fd(descriptor, key, indexed, depth=key.count("/"))
        with self._lock:
            self._directories.update(indexed)
            self._dirty.difference_update(indexed)
            for indexed_key, record in indexed.items():
                _server_id, separator, relative = indexed_key.partition("/")
                parent = relative.rpartition("/")[0] if "/" in relative else ("" if separator else None)
                self._queue_sync_locked(indexed_key, {
                    "path": relative if separator else "",
                    "parent": parent,
                    "entries": record["entries"],
                })
            self._needs_persist = True
            self._persist_locked()
            self._needs_persist = False
            self._last_persist = time.monotonic()

    def _prune_subtree(self, key: str) -> None:
        prefix = key + "/"
        with self._lock:
            removed = [path for path in self._directories if path == key or path.startswith(prefix)]
            for path in removed:
                self._directories.pop(path, None)
                self._dirty.discard(path)
                self._queue_sync_locked(path, None)
            if removed:
                self._needs_persist = True
            watches = [(wd, path) for wd, path in self._watch_paths.items()
                       if path == key or path.startswith(prefix)]
        for wd, _path in watches:
            if self._fd is not None and self._libc is not None:
                self._libc.inotify_rm_watch(self._fd, wd)
        if removed:
            with self._lock:
                self._persist_locked()
                self._needs_persist = False
                self._last_persist = time.monotonic()

    def _refresh_directory(self, key: str) -> None:
        if not self._valid_key(key):
            return
        try:
            descriptor = self._open_key(key)
        except OSError:
            self._prune_subtree(key)
            return
        try:
            info = os.fstat(descriptor)
            entries = self.scan_directory(descriptor)
            with self._lock:
                self._directories[key] = {"mtime_ns": info.st_mtime_ns, "entries": entries}
                self._dirty.discard(key)
                relative = key.partition("/")[2] if "/" in key else ""
                parent = relative.rpartition("/")[0] if "/" in relative else ("" if "/" in key else None)
                self._queue_sync_locked(key, {"path": relative, "parent": parent, "entries": entries})
                self._add_watch_fd(descriptor, key)
                self._needs_persist = True
                self._persist_locked()
                self._needs_persist = False
                self._last_persist = time.monotonic()
        finally:
            os.close(descriptor)

    def _persist_locked(self) -> None:
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            document = {"version": INDEX_VERSION, "directories": self._directories}
            encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_INDEX_BYTES:
                LOGGER.warning("file_tree_index_too_large bytes=%d", len(encoded))
                return
            fd, temp_name = tempfile.mkstemp(prefix=".file-tree-", suffix=".tmp", dir=self.state_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), 0o600)
                os.replace(temp_name, self.index_path)
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            LOGGER.warning("file_tree_index_persist_failed error=%s", str(exc)[:200])

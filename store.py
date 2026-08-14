"""Where Imposter tables live between requests.

The game used to keep every room in one process's memory. That is fine while a
laptop hosts the LAN game and never restarts, but on a serverless host each
request can land on a different (or brand new) instance, so a phone that comes
back from a locked screen — or a friend who scans the QR a few minutes late —
would be told the session expired even though the game was still going.

Three backends share one small interface:

* ``MemoryStore`` (in ``engine``) — single process, used by tests and LAN play.
* ``SqliteStore`` — a file, so a table survives a restart of the server.
* ``RedisRestStore`` — an Upstash-style REST cache shared by every instance,
  which is what a serverless deployment needs. It speaks HTTP through httpx,
  so it needs no extra dependency.

``create_store()`` picks one from the environment, and every backend degrades
loudly (``StoreUnavailable``) instead of quietly starting a second, empty copy
of the game.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from engine import (
    ROOM_IDLE_SECONDS,
    MemoryStore,
    Room,
    RoomStore,
    StoreConflict,
    StoreUnavailable,
    room_from_dict,
    room_to_dict,
)

ROOM_KEY_PREFIX = "imposter:room:"
REDIS_TIMEOUT_SECONDS = 4.0
# Upstash and Vercel KV both expose the same REST shape under different names.
REDIS_ENV_PAIRS = (
    ("IMPOSTER_REDIS_REST_URL", "IMPOSTER_REDIS_REST_TOKEN"),
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
)
# Hosts that answer one request per instance and hand each instance its own
# disk. A file-backed store there is private to whichever instance wrote it.
FLEET_ENV_KEYS = (
    "VERCEL",
    "AWS_LAMBDA_FUNCTION_NAME",
    "FUNCTIONS_WORKER_RUNTIME",
    "K_SERVICE",
    "FLY_ALLOC_ID",
)

_CAS_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
  local sep = string.find(current, '\\n', 1, true)
  local version = tonumber(string.sub(current, 1, sep - 1))
  if version ~= tonumber(ARGV[1]) then return 0 end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
return 1
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    code TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    last_seen REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rooms_last_seen ON rooms (last_seen);
"""


def room_ttl_seconds() -> float:
    """How long an untouched table is kept. Polling phones keep theirs alive."""
    raw = (os.getenv("IMPOSTER_ROOM_TTL_SECONDS") or "").strip()
    if not raw:
        return float(ROOM_IDLE_SECONDS)
    try:
        seconds = float(raw)
    except ValueError:
        return float(ROOM_IDLE_SECONDS)
    return seconds if seconds > 0 else float(ROOM_IDLE_SECONDS)


def runs_as_a_fleet() -> bool:
    """True when more than one copy of this app answers the same game.

    ``IMPOSTER_MULTI_INSTANCE`` settles it either way for a host we do not
    recognise, such as a container behind a load balancer.
    """
    explicit = (os.getenv("IMPOSTER_MULTI_INSTANCE") or "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return any((os.getenv(key) or "").strip() for key in FLEET_ENV_KEYS)


def redis_rest_config() -> tuple[str, str] | None:
    for url_key, token_key in REDIS_ENV_PAIRS:
        url = (os.getenv(url_key) or "").strip().rstrip("/")
        token = (os.getenv(token_key) or "").strip()
        if url and token:
            return url, token
    return None


def default_room_db_path() -> Path:
    """Rooms sit next to the word bank, which already knows about /tmp on Vercel."""
    override = (os.getenv("IMPOSTER_ROOM_DB_PATH") or "").strip()
    if override:
        return Path(override)
    from wordbank import default_db_path

    return default_db_path()


class SqliteStore:
    """A table survives a restart of the process that is hosting the game."""

    kind = "sqlite"

    def __init__(self, db_path: str | Path | None = None, ttl_seconds: float | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_room_db_path()
        self.ttl_seconds = room_ttl_seconds() if ttl_seconds is None else ttl_seconds
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # Some filesystems refuse WAL; the default journal still works.
            pass
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def load(self, code: str) -> Room | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT version, last_seen, data FROM rooms WHERE code = ?", (code,)
            ).fetchone()
        if row is None:
            return None
        if row["last_seen"] < time.time() - self.ttl_seconds:
            self.delete(code)
            return None
        room = room_from_dict(json.loads(row["data"]))
        room.version = int(row["version"])
        return room

    def save(self, room: Room) -> None:
        expected = room.version
        payload = json.dumps(room_to_dict(room))
        with self._lock, self.conn:
            updated = self.conn.execute(
                "UPDATE rooms SET version = ?, last_seen = ?, data = ? "
                "WHERE code = ? AND version = ?",
                (expected + 1, room.last_seen_at, payload, room.code, expected),
            )
            if updated.rowcount == 0:
                if self._exists(room.code):
                    raise StoreConflict(room.code)
                try:
                    self.conn.execute(
                        "INSERT INTO rooms (code, version, last_seen, data) VALUES (?, ?, ?, ?)",
                        (room.code, expected + 1, room.last_seen_at, payload),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreConflict(room.code) from exc
        room.version = expected + 1

    def delete(self, code: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM rooms WHERE code = ?", (code,))

    def sweep(self, older_than: float) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM rooms WHERE last_seen < ?", (older_than,))

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _exists(self, code: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM rooms WHERE code = ?", (code,)).fetchone()
        return row is not None


class RedisRestStore:
    """Every instance of a serverless deployment reads the same table."""

    kind = "redis"

    def __init__(
        self,
        url: str,
        token: str,
        ttl_seconds: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.ttl_seconds = room_ttl_seconds() if ttl_seconds is None else ttl_seconds
        self.client = client or httpx.Client(timeout=REDIS_TIMEOUT_SECONDS)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._compare_and_set = True

    def load(self, code: str) -> Room | None:
        raw = self._command(["GET", self._key(code)])
        if not raw:
            return None
        version, _, payload = str(raw).partition("\n")
        if not payload:
            return None
        room = room_from_dict(json.loads(payload))
        room.version = int(version or 0)
        return room

    def save(self, room: Room) -> None:
        """Write the room, refusing the write if another instance got there first.

        A cache that cannot run the script (an older or cut-down Redis) keeps
        the game going with a plain write instead: a lost race beats a table
        nobody can reach.
        """
        expected = room.version
        payload = f"{expected + 1}\n{json.dumps(room_to_dict(room))}"
        ttl = str(int(self.ttl_seconds))
        if self._compare_and_set:
            written = self._command(
                ["EVAL", _CAS_SCRIPT, "1", self._key(room.code), str(expected), payload, ttl],
                on_script_error=self._drop_compare_and_set,
            )
            if written is not None:
                if not int(written or 0):
                    raise StoreConflict(room.code)
                room.version = expected + 1
                return
        self._command(["SET", self._key(room.code), payload, "EX", ttl])
        room.version = expected + 1

    def delete(self, code: str) -> None:
        self._command(["DEL", self._key(code)])

    def sweep(self, older_than: float) -> None:
        """Redis expires idle rooms on its own; every write refreshes the TTL."""

    def close(self) -> None:
        self.client.close()

    def _drop_compare_and_set(self) -> None:
        self._compare_and_set = False

    def _key(self, code: str) -> str:
        return f"{ROOM_KEY_PREFIX}{code}"

    def _command(self, parts: list[Any], on_script_error: Any = None) -> Any:
        try:
            response = self.client.post(self.url, headers=self._headers, json=parts)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise StoreUnavailable(str(exc)) from exc
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            # The cache answered but disliked the command, which is a problem
            # with the command rather than with reaching the cache.
            if on_script_error is not None and response.status_code < 400:
                on_script_error()
                return None
            raise StoreUnavailable(str(error))
        if response.status_code >= 400:
            raise StoreUnavailable(f"room store returned {response.status_code}")
        return body.get("result") if isinstance(body, dict) else None


@dataclass(frozen=True)
class StoreInfo:
    """What a table can expect from where it is being kept."""

    kind: str
    shared: bool
    detail: str


def describe_store(store: RoomStore) -> StoreInfo:
    """Report whether every instance answering this game sees the same tables.

    A store that only one instance can read is the difference between a table
    that stays open and one that closes at random, so it is worth saying out
    loud rather than leaving it to be discovered mid-game.
    """
    kind = getattr(store, "kind", type(store).__name__)
    if kind == "redis":
        return StoreInfo(kind, True, "Tables are kept in a shared cache.")
    fleet = runs_as_a_fleet()
    if kind == "sqlite":
        if not fleet:
            return StoreInfo(kind, True, "Tables are kept in a file and survive a restart.")
        return StoreInfo(
            kind,
            False,
            "This host runs more than one copy of the game and gives each one its own "
            "disk, so a table is only visible to the copy that created it. Add a Redis "
            "cache (KV_REST_API_URL and KV_REST_API_TOKEN) to share tables.",
        )
    detail = "Tables are kept in memory and are lost when this process stops."
    if fleet:
        detail += (
            " This host also runs more than one copy of the game, so a table is only "
            "visible to the copy that created it."
        )
    return StoreInfo(kind, not fleet, detail)


def create_store(ttl_seconds: float | None = None) -> RoomStore:
    """Pick a room store from the environment.

    ``IMPOSTER_ROOM_STORE`` forces one of ``memory``, ``sqlite`` or ``redis``.
    Left alone, a shared Redis cache wins when one is configured, otherwise
    rooms go in the SQLite file, and a read-only filesystem falls back to
    memory so the game still runs.
    """
    ttl = room_ttl_seconds() if ttl_seconds is None else ttl_seconds
    kind = (os.getenv("IMPOSTER_ROOM_STORE") or "auto").strip().lower()
    config = redis_rest_config()

    if kind == "memory":
        return MemoryStore()
    if kind == "redis" or (kind == "auto" and config):
        if not config:
            raise RuntimeError(
                "IMPOSTER_ROOM_STORE=redis needs KV_REST_API_URL and KV_REST_API_TOKEN."
            )
        return RedisRestStore(config[0], config[1], ttl_seconds=ttl)
    try:
        return SqliteStore(ttl_seconds=ttl)
    except (OSError, sqlite3.Error):
        if kind == "sqlite":
            raise
        return MemoryStore()

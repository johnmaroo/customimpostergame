import json
import socket
import time
import unittest
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import httpx

import store as store_module
from engine import (
    GameError,
    GameHub,
    MemoryStore,
    Room,
    RoundState,
    StoreConflict,
    StoreUnavailable,
    room_from_dict,
    room_to_dict,
    serialized_room_fields,
    serialized_round_fields,
)
from store import (
    RedisRestStore,
    RedisUrlStore,
    SqliteStore,
    create_store,
    describe_store,
    room_ttl_seconds,
)


def played_room(hub: GameHub) -> tuple[Room, list]:
    """A room mid-round, so serialization has every kind of state to carry."""
    room, host = hub.create_room("Host")
    _, ava = hub.join_room(room.code, "Ava")
    _, ben = hub.join_room(room.code, "Ben")
    hub.add_invite(room, host, "Jordan", "+15551234567")
    for word in ("Toaster", "Volcano", "Sandcastle"):
        hub.add_word(room, host, word)
    hub.start_round(room, host)
    hub.mark_ready(room, host)
    return room, [host, ava, ben]


def guessing_room(hub: GameHub) -> tuple[Room, list]:
    """A table carried through the circle to the imposters' one guess.

    Two imposters, one of whom has already missed, so the round is caught
    mid-guess rather than resolved.
    """
    room, host = hub.create_room("Host")
    _, ava = hub.join_room(room.code, "Ava")
    _, ben = hub.join_room(room.code, "Ben")
    hub.set_settings(room, host, num_imposters=2)
    for word in ("Toaster", "Volcano", "Sandcastle"):
        hub.add_word(room, host, word)
    hub.start_round(room, host)
    players = [host, ava, ben]
    hub.advance(room, host)  # reveal -> the speaking circle
    hub.go_around_again(room, host)
    hub.advance(room, host)  # -> open floor
    hub.advance(room, host)  # -> guessing
    misser = next(p for p in players if p.id in room.round.imposter_ids)
    hub.guess_word(room, misser, "definitely not the word")
    return room, players


class RoomSerializationTests(unittest.TestCase):
    def test_every_room_field_is_written(self) -> None:
        declared = {f.name for f in fields(Room)}
        self.assertEqual(declared, serialized_room_fields())

    def test_every_round_field_is_written(self) -> None:
        declared = {f.name for f in fields(RoundState)}
        self.assertEqual(declared, serialized_round_fields())

    def test_round_trip_keeps_the_table_intact(self) -> None:
        hub = GameHub()
        room, players = played_room(hub)
        restored = room_from_dict(json.loads(json.dumps(room_to_dict(room))))

        self.assertEqual(restored.code, room.code)
        self.assertEqual(restored.phase, room.phase)
        self.assertEqual(sorted(restored.players), sorted(room.players))
        self.assertEqual(restored.remaining_words, room.remaining_words)
        self.assertEqual(restored.used_words, room.used_words)
        self.assertEqual(restored.round.word, room.round.word)
        self.assertEqual(restored.round.imposter_ids, room.round.imposter_ids)
        self.assertEqual(restored.round.speaking_order, room.round.speaking_order)
        self.assertEqual(restored.round.ready_ids, room.round.ready_ids)
        self.assertEqual(restored.round.prompt, room.round.prompt)
        self.assertEqual(list(restored.invites), list(room.invites))
        self.assertEqual(restored.imposter_counts, room.imposter_counts)
        self.assertEqual(restored.starter_counts, room.starter_counts)
        for player in players:
            self.assertEqual(restored.players[player.id].name, player.name)
            self.assertEqual(restored.players[player.id].score, player.score)

    def test_a_half_finished_guess_round_round_trips(self) -> None:
        hub = GameHub()
        room, _players = guessing_room(hub)
        restored = room_from_dict(json.loads(json.dumps(room_to_dict(room))))

        self.assertEqual(restored.phase, "guess")
        self.assertEqual(restored.round.lap, room.round.lap)
        self.assertEqual(restored.round.guesses, room.round.guesses)
        self.assertEqual(restored.round.clue_unlocked, room.round.clue_unlocked)
        self.assertEqual(restored.round.win_reason, room.round.win_reason)
        self.assertEqual(restored.round.guessed_by, room.round.guessed_by)
        self.assertEqual(restored.round.speaking_order, room.round.speaking_order)

    def test_bearer_tokens_are_never_written_down(self) -> None:
        hub = GameHub()
        room, players = played_room(hub)
        blob = json.dumps(room_to_dict(room))
        for player in players:
            self.assertNotIn(player.token, blob)
            self.assertIn(player.token_hash, blob)
        self.assertEqual(room_from_dict(json.loads(blob)).players[players[0].id].token, "")


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "rooms.db"
        self.store = SqliteStore(self.path)
        self.addCleanup(self.store.close)

    def test_a_table_survives_a_restart_of_the_server(self) -> None:
        hub = GameHub(store=self.store)
        room, players = played_room(hub)
        host = players[0]

        restarted = GameHub(store=SqliteStore(self.path))
        found_room, found_player = restarted.resolve_token(host.token)
        self.assertEqual(found_room.code, room.code)
        self.assertEqual(found_player.id, host.id)
        self.assertEqual(found_room.phase, "reveal")
        self.assertEqual(len(found_room.players), 3)

    def test_a_guest_can_join_from_another_process(self) -> None:
        hub = GameHub(store=self.store)
        room, _ = hub.create_room("Host")

        other = GameHub(store=SqliteStore(self.path))
        joined, player = other.join_room(room.code, "Ava")
        self.assertEqual(joined.code, room.code)

        back = GameHub(store=SqliteStore(self.path))
        seen, _ = back.resolve_token(player.token)
        self.assertEqual(sorted(p.name for p in seen.players.values()), ["Ava", "Host"])

    def test_a_stale_writer_is_asked_to_retry(self) -> None:
        hub = GameHub(store=self.store)
        room, players = played_room(hub)
        stale = SqliteStore(self.path).load(room.code)

        hub.advance(room, players[0])
        stale.round.speaker_index = 2
        with self.assertRaises(StoreConflict):
            self.store.save(stale)

    def test_an_abandoned_table_is_swept_but_a_watched_one_is_not(self) -> None:
        store = SqliteStore(self.path, ttl_seconds=60)
        hub = GameHub(store=store, idle_seconds=60)
        watched, players = played_room(hub)
        abandoned, _ = hub.create_room("Ghost")
        abandoned_code = abandoned.code

        later = time.time() + 3600
        hub.resolve_token(players[0].token, now=later)
        hub.sweep_idle(now=later)

        self.assertIsNotNone(store.load(watched.code))
        self.assertIsNone(store.load(abandoned_code))

    def test_an_expired_room_reads_as_gone(self) -> None:
        store = SqliteStore(self.path, ttl_seconds=0.01)
        hub = GameHub(store=store)
        room, _ = hub.create_room("Host")
        time.sleep(0.02)
        self.assertIsNone(store.load(room.code))


class FakeRedis:
    """Enough of the Upstash REST API to exercise the client."""

    def __init__(self, *, eval_supported: bool = True) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.eval_supported = eval_supported
        self.commands: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = json.loads(request.content)
        self.commands.append(parts)
        name = parts[0].upper()
        if name == "GET":
            return httpx.Response(200, json={"result": self.values.get(parts[1])})
        if name == "SET":
            self.values[parts[1]] = parts[2]
            if len(parts) > 4:
                self.expiries[parts[1]] = int(parts[4])
            return httpx.Response(200, json={"result": "OK"})
        if name == "DEL":
            self.values.pop(parts[1], None)
            return httpx.Response(200, json={"result": 1})
        if name == "EVAL":
            if not self.eval_supported:
                return httpx.Response(200, json={"error": "ERR unknown command 'EVAL'"})
            _script, _keys, key, expected, payload, ttl = parts[1:]
            current = self.values.get(key)
            if current is not None:
                version = int(current.split("\n", 1)[0])
                if version != int(expected):
                    return httpx.Response(200, json={"result": 0})
            self.values[key] = payload
            self.expiries[key] = int(ttl)
            return httpx.Response(200, json={"result": 1})
        return httpx.Response(200, json={"error": f"ERR unknown command '{name}'"})


def redis_store(fake: FakeRedis, **kwargs) -> RedisRestStore:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return RedisRestStore("https://redis.example", "token", client=client, **kwargs)


class RedisRestStoreTests(unittest.TestCase):
    def test_two_instances_share_one_table(self) -> None:
        fake = FakeRedis()
        hub = GameHub(store=redis_store(fake))
        room, players = played_room(hub)

        second_instance = GameHub(store=redis_store(fake))
        seen_room, seen_player = second_instance.resolve_token(players[1].token)
        self.assertEqual(seen_room.code, room.code)
        self.assertEqual(seen_player.name, "Ava")
        self.assertEqual(seen_room.round.word, room.round.word)

    def test_rooms_are_written_with_an_expiry(self) -> None:
        fake = FakeRedis()
        hub = GameHub(store=redis_store(fake, ttl_seconds=900))
        room, _ = hub.create_room("Host")
        self.assertEqual(fake.expiries[f"imposter:room:{room.code}"], 900)

    def test_a_stale_writer_is_asked_to_retry(self) -> None:
        fake = FakeRedis()
        store = redis_store(fake)
        hub = GameHub(store=store)
        room, players = played_room(hub)
        stale = redis_store(fake).load(room.code)

        hub.advance(room, players[0])
        stale.round.speaker_index = 2
        with self.assertRaises(StoreConflict):
            store.save(stale)

    def test_a_cache_without_scripting_still_hosts_the_game(self) -> None:
        fake = FakeRedis(eval_supported=False)
        hub = GameHub(store=redis_store(fake))
        room, _players = played_room(hub)
        reread = redis_store(fake).load(room.code)
        self.assertEqual(sorted(reread.players), sorted(room.players))
        self.assertEqual(reread.phase, room.phase)
        tried = [parts[0] for parts in fake.commands]
        self.assertIn("EVAL", tried)
        self.assertIn("SET", tried)

    def test_an_unreachable_store_says_so(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        store = RedisRestStore(
            "https://redis.example",
            "token",
            client=httpx.Client(transport=httpx.MockTransport(refuse)),
        )
        with self.assertRaises(StoreUnavailable):
            store.load("KNTQ")

    def test_a_rejected_token_says_so(self) -> None:
        def deny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "Unauthorized"})

        store = RedisRestStore(
            "https://redis.example",
            "token",
            client=httpx.Client(transport=httpx.MockTransport(deny)),
        )
        with self.assertRaises(StoreUnavailable):
            store.load("KNTQ")


def redis_is_running(host: str = "127.0.0.1", port: int = 6379) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3) as sock:
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            return sock.recv(64).startswith(b"+PONG")
    except OSError:
        return False


class ResplessRedis:
    """Speaks the REST shape of Upstash on top of a plain Redis connection.

    The mock above proves the client's bookkeeping; this proves the commands
    and the compare-and-set script are the ones Redis actually understands.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6379) -> None:
        self.sock = socket.create_connection((host, port), timeout=2)
        self.buffer = b""

    def close(self) -> None:
        self.sock.close()

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = [str(part) for part in json.loads(request.content)]
        wire = f"*{len(parts)}\r\n".encode()
        for part in parts:
            raw = part.encode()
            wire += b"$%d\r\n%s\r\n" % (len(raw), raw)
        self.sock.sendall(wire)
        try:
            return httpx.Response(200, json={"result": self._reply()})
        except RuntimeError as exc:
            return httpx.Response(200, json={"error": str(exc)})

    def _line(self) -> bytes:
        while b"\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ERR connection closed")
            self.buffer += chunk
        line, _, rest = self.buffer.partition(b"\r\n")
        self.buffer = rest
        return line

    def _reply(self) -> Any:
        line = self._line()
        kind, body = line[:1], line[1:]
        if kind in (b"+", b":"):
            return int(body) if kind == b":" else body.decode()
        if kind == b"-":
            raise RuntimeError(body.decode())
        if kind == b"$":
            size = int(body)
            if size < 0:
                return None
            while len(self.buffer) < size + 2:
                self.buffer += self.sock.recv(4096)
            value, self.buffer = self.buffer[:size], self.buffer[size + 2 :]
            return value.decode()
        if kind == b"*":
            return [self._reply() for _ in range(max(0, int(body)))]
        raise RuntimeError(f"ERR unexpected reply {line!r}")


@unittest.skipUnless(redis_is_running(), "no Redis on 127.0.0.1:6379")
class RealRedisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = ResplessRedis()
        self.addCleanup(self.wire.close)

    def store(self, **kwargs) -> RedisRestStore:
        client = httpx.Client(transport=httpx.MockTransport(self.wire.handler))
        return RedisRestStore("https://redis.example", "token", client=client, **kwargs)

    def test_two_instances_share_one_table(self) -> None:
        store = self.store()
        hub = GameHub(store=store)
        room, players = played_room(hub)
        self.addCleanup(store.delete, room.code)

        other = GameHub(store=self.store())
        seen_room, seen_player = other.resolve_token(players[1].token)
        self.assertEqual(seen_room.code, room.code)
        self.assertEqual(seen_player.name, "Ava")
        self.assertEqual(seen_room.round.word, room.round.word)

    def test_the_script_refuses_a_stale_write(self) -> None:
        store = self.store()
        hub = GameHub(store=store)
        room, players = played_room(hub)
        self.addCleanup(store.delete, room.code)
        stale = self.store().load(room.code)

        hub.advance(room, players[0])
        with self.assertRaises(StoreConflict):
            store.save(stale)
        self.assertEqual(self.store().load(room.code).phase, "discuss")

    def test_a_deleted_table_reads_as_gone(self) -> None:
        store = self.store()
        hub = GameHub(store=store)
        room, _ = hub.create_room("Host")
        store.delete(room.code)
        self.assertIsNone(store.load(room.code))


try:  # Optional: gives the Lua a real interpreter without a Redis to talk to.
    import fakeredis
except ImportError:  # pragma: no cover - depends on what is installed
    fakeredis = None


@unittest.skipUnless(fakeredis, "fakeredis is not installed")
class LuaCompareAndSetTests(unittest.TestCase):
    """Run the version check through an actual Lua interpreter.

    The other suites reproduce the script's logic in Python, which proves the
    store around it but would not catch a mistake inside the script itself.
    """

    def store(self, client) -> RedisUrlStore:
        return RedisUrlStore("redis://fake", client=client)

    def test_a_stale_write_is_refused_by_the_script(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        store = self.store(client)
        hub = GameHub(store=store)
        room, players = played_room(hub)
        stale = self.store(client).load(room.code)

        hub.advance(room, players[0])
        with self.assertRaises(StoreConflict):
            store.save(stale)
        self.assertEqual(self.store(client).load(room.code).phase, "discuss")

    def test_a_fresh_write_is_allowed_and_keeps_the_expiry(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        store = self.store(client)
        hub = GameHub(store=store)
        room, players = played_room(hub)

        hub.advance(room, players[0])
        left = client.ttl(f"imposter:room:{room.code}")
        self.assertGreater(left, 0)
        self.assertEqual(self.store(client).load(room.code).phase, "discuss")

    def test_a_second_instance_reads_the_same_table(self) -> None:
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        hub = GameHub(store=self.store(client))
        room, players = played_room(hub)

        other = GameHub(store=self.store(client))
        seen_room, seen_player = other.resolve_token(players[1].token)
        self.assertEqual(seen_room.code, room.code)
        self.assertEqual(seen_player.name, "Ava")
        self.assertEqual(seen_room.round.word, room.round.word)


@unittest.skipUnless(redis_is_running(), "no Redis on 127.0.0.1:6379")
class RealRedisUrlTests(unittest.TestCase):
    """The connection-string path against a real server, Lua and all."""

    def store(self, **kwargs) -> RedisUrlStore:
        store = RedisUrlStore("redis://127.0.0.1:6379", **kwargs)
        self.addCleanup(store.close)
        return store

    def test_two_instances_share_one_table(self) -> None:
        store = self.store()
        hub = GameHub(store=store)
        room, players = played_room(hub)
        self.addCleanup(store.delete, room.code)

        other = GameHub(store=self.store())
        seen_room, seen_player = other.resolve_token(players[1].token)
        self.assertEqual(seen_room.code, room.code)
        self.assertEqual(seen_player.name, "Ava")
        self.assertEqual(seen_room.round.word, room.round.word)

    def test_the_script_refuses_a_stale_write(self) -> None:
        store = self.store()
        hub = GameHub(store=store)
        room, players = played_room(hub)
        self.addCleanup(store.delete, room.code)
        stale = self.store().load(room.code)

        hub.advance(room, players[0])
        with self.assertRaises(StoreConflict):
            store.save(stale)
        self.assertEqual(self.store().load(room.code).phase, "discuss")

    def test_rooms_are_written_with_an_expiry(self) -> None:
        store = self.store(ttl_seconds=900)
        hub = GameHub(store=store)
        room, _ = hub.create_room("Host")
        self.addCleanup(store.delete, room.code)
        left = store.client.ttl(f"imposter:room:{room.code}")
        self.assertGreater(left, 0)
        self.assertLessEqual(left, 900)

    def test_a_rest_written_table_reads_back_over_a_connection(self) -> None:
        """The two backends are the same table, not two encodings of one."""
        wire = ResplessRedis()
        self.addCleanup(wire.close)
        rest = RedisRestStore(
            "https://redis.example",
            "token",
            client=httpx.Client(transport=httpx.MockTransport(wire.handler)),
        )
        hub = GameHub(store=rest)
        room, players = played_room(hub)
        self.addCleanup(rest.delete, room.code)

        over_url = GameHub(store=self.store())
        seen_room, seen_player = over_url.resolve_token(players[1].token)
        self.assertEqual(seen_room.code, room.code)
        self.assertEqual(seen_room.round.word, room.round.word)
        self.assertEqual(seen_player.name, "Ava")


class StoreChoiceTests(unittest.TestCase):
    def test_memory_is_explicit(self) -> None:
        with patch.dict("os.environ", {"IMPOSTER_ROOM_STORE": "memory"}, clear=False):
            self.assertIsInstance(create_store(), MemoryStore)

    def test_a_shared_cache_wins_when_configured(self) -> None:
        env = {
            "KV_REST_API_URL": "https://redis.example",
            "KV_REST_API_TOKEN": "secret",
        }
        with patch.dict("os.environ", env, clear=False):
            store = create_store()
        self.assertIsInstance(store, RedisRestStore)
        store.close()

    def test_a_connection_string_is_enough_on_its_own(self) -> None:
        """Some providers hand out no REST endpoint at all."""
        env = {**NO_REDIS, "REDIS_URL": "rediss://default:pw@redis.example:6379"}
        with patch.dict("os.environ", env, clear=False):
            store = create_store()
        self.assertIsInstance(store, RedisUrlStore)

    def test_rest_is_preferred_when_a_provider_offers_both(self) -> None:
        """REST holds no socket open, which suits a host that runs many copies."""
        env = {
            "REDIS_URL": "rediss://default:pw@redis.example:6379",
            "KV_REST_API_URL": "https://redis.example",
            "KV_REST_API_TOKEN": "secret",
        }
        with patch.dict("os.environ", env, clear=False):
            store = create_store()
        self.assertIsInstance(store, RedisRestStore)
        store.close()

    def test_a_connection_string_that_is_not_one_is_ignored(self) -> None:
        env = {**NO_REDIS, "REDIS_URL": "redis.example:6379"}
        with TemporaryDirectory() as folder:
            env["IMPOSTER_ROOM_DB_PATH"] = str(Path(folder) / "rooms.db")
            with patch.dict("os.environ", env, clear=False):
                store = create_store()
            self.assertIsInstance(store, SqliteStore)
            store.close()

    def test_forcing_redis_without_any_credentials_is_refused(self) -> None:
        env = {**NO_REDIS, "IMPOSTER_ROOM_STORE": "redis", "REDIS_URL": "", "KV_URL": ""}
        with patch.dict("os.environ", env, clear=False), self.assertRaises(RuntimeError) as caught:
            create_store()
        self.assertIn("REDIS_URL", str(caught.exception))

    def test_redis_without_credentials_is_refused(self) -> None:
        env = {
            "IMPOSTER_ROOM_STORE": "redis",
            "KV_REST_API_URL": "",
            "KV_REST_API_TOKEN": "",
            "UPSTASH_REDIS_REST_URL": "",
            "UPSTASH_REDIS_REST_TOKEN": "",
            "IMPOSTER_REDIS_REST_URL": "",
            "IMPOSTER_REDIS_REST_TOKEN": "",
        }
        with patch.dict("os.environ", env, clear=False), self.assertRaises(RuntimeError):
            create_store()

    def test_a_file_is_the_default(self) -> None:
        with TemporaryDirectory() as folder:
            env = {
                "IMPOSTER_ROOM_STORE": "auto",
                "IMPOSTER_ROOM_DB_PATH": str(Path(folder) / "rooms.db"),
                "KV_REST_API_URL": "",
                "KV_REST_API_TOKEN": "",
                "UPSTASH_REDIS_REST_URL": "",
                "UPSTASH_REDIS_REST_TOKEN": "",
                "IMPOSTER_REDIS_REST_URL": "",
                "IMPOSTER_REDIS_REST_TOKEN": "",
            }
            with patch.dict("os.environ", env, clear=False):
                store = create_store()
            self.assertIsInstance(store, SqliteStore)
            store.close()

    def test_the_table_lifetime_is_configurable(self) -> None:
        with patch.dict("os.environ", {"IMPOSTER_ROOM_TTL_SECONDS": "600"}, clear=False):
            self.assertEqual(room_ttl_seconds(), 600)
        with patch.dict("os.environ", {"IMPOSTER_ROOM_TTL_SECONDS": "nonsense"}, clear=False):
            self.assertEqual(room_ttl_seconds(), store_module.ROOM_IDLE_SECONDS)


class FakeRedisClient:
    """Enough of redis-py to exercise the connection-string store.

    The Lua is not run; the check it performs is reproduced here so a stale
    write is refused the same way a real server would refuse it.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.fail_with = fail_with
        self.closed = False

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get(self, key: str) -> str | None:
        self._maybe_fail()
        return self.values.get(key)

    def eval(self, _script: str, _numkeys: int, key: str, expected: str, payload: str, ttl: str):
        self._maybe_fail()
        current = self.values.get(key)
        if current is not None and int(current.split("\n", 1)[0]) != int(expected):
            return 0
        self.values[key] = payload
        self.expiries[key] = int(ttl)
        return 1

    def delete(self, key: str) -> int:
        self._maybe_fail()
        self.values.pop(key, None)
        return 1

    def close(self) -> None:
        self.closed = True


class RedisUrlStoreTests(unittest.TestCase):
    """Some providers only offer a connection string, with no REST endpoint."""

    def store(self, client: FakeRedisClient, **kwargs) -> RedisUrlStore:
        return RedisUrlStore("rediss://default:pw@redis.example:6379", client=client, **kwargs)

    def test_two_instances_share_one_table(self) -> None:
        client = FakeRedisClient()
        hub = GameHub(store=self.store(client))
        room, players = played_room(hub)

        other = GameHub(store=self.store(client))
        seen_room, seen_player = other.resolve_token(players[1].token)
        self.assertEqual(seen_room.code, room.code)
        self.assertEqual(seen_player.name, "Ava")
        self.assertEqual(seen_room.round.word, room.round.word)

    def test_rooms_are_written_with_an_expiry(self) -> None:
        client = FakeRedisClient()
        hub = GameHub(store=self.store(client, ttl_seconds=900))
        room, _ = hub.create_room("Host")
        self.assertEqual(client.expiries[f"imposter:room:{room.code}"], 900)

    def test_a_stale_writer_is_asked_to_retry(self) -> None:
        client = FakeRedisClient()
        store = self.store(client)
        hub = GameHub(store=store)
        room, players = played_room(hub)
        stale = self.store(client).load(room.code)

        hub.advance(room, players[0])
        with self.assertRaises(StoreConflict):
            store.save(stale)
        self.assertEqual(self.store(client).load(room.code).phase, "discuss")

    def test_a_deleted_table_reads_as_gone(self) -> None:
        client = FakeRedisClient()
        store = self.store(client)
        hub = GameHub(store=store)
        room, _ = hub.create_room("Host")
        store.delete(room.code)
        self.assertIsNone(store.load(room.code))

    def test_an_unreachable_store_says_so(self) -> None:
        import redis as redis_py

        store = self.store(FakeRedisClient(fail_with=redis_py.ConnectionError("refused")))
        with self.assertRaises(StoreUnavailable):
            store.load("ABCD")

    def test_a_dropped_socket_says_so_too(self) -> None:
        store = self.store(FakeRedisClient(fail_with=OSError("broken pipe")))
        with self.assertRaises(StoreUnavailable):
            store.load("ABCD")

    def test_the_two_backends_write_the_same_bytes(self) -> None:
        """A deployment can move between REST and a connection string."""
        rest_fake = FakeRedis()
        rest = redis_store(rest_fake)
        client = FakeRedisClient()
        url = self.store(client)

        room, _ = GameHub(store=rest).create_room("Host")
        carried = GameHub(store=url)
        carried.store.save(rest.load(room.code))

        key = f"imposter:room:{room.code}"
        written = json.loads(client.values[key].split("\n", 1)[1])
        original = json.loads(rest_fake.values[key].split("\n", 1)[1])
        # Everything but the version, which a second write is meant to move on.
        self.assertEqual(written.pop("version"), original.pop("version") + 1)
        self.assertEqual(written, original)


NO_REDIS = {
    "KV_REST_API_URL": "",
    "KV_REST_API_TOKEN": "",
    "UPSTASH_REDIS_REST_URL": "",
    "UPSTASH_REDIS_REST_TOKEN": "",
    "IMPOSTER_REDIS_REST_URL": "",
    "IMPOSTER_REDIS_REST_TOKEN": "",
}


class SharingReportTests(unittest.TestCase):
    """A store only one instance can read is why a table closes at random."""

    def _sqlite(self, folder: str) -> SqliteStore:
        return SqliteStore(Path(folder) / "rooms.db")

    def test_a_file_is_shared_on_one_machine(self) -> None:
        with TemporaryDirectory() as folder, patch.dict(
            "os.environ", {**NO_REDIS, "IMPOSTER_MULTI_INSTANCE": "0", "VERCEL": ""}, clear=False
        ):
            store = self._sqlite(folder)
            info = describe_store(store)
            store.close()
        self.assertEqual(info.kind, "sqlite")
        self.assertTrue(info.shared)

    def test_a_file_is_not_shared_across_a_fleet(self) -> None:
        with TemporaryDirectory() as folder, patch.dict(
            "os.environ", {**NO_REDIS, "VERCEL": "1"}, clear=False
        ):
            store = self._sqlite(folder)
            info = describe_store(store)
            store.close()
        self.assertFalse(info.shared)
        self.assertIn("own disk", info.detail)

    def test_an_unknown_host_can_say_it_runs_more_than_one_copy(self) -> None:
        with TemporaryDirectory() as folder, patch.dict(
            "os.environ", {**NO_REDIS, "IMPOSTER_MULTI_INSTANCE": "yes"}, clear=False
        ):
            store = self._sqlite(folder)
            info = describe_store(store)
            store.close()
        self.assertFalse(info.shared)

    def test_a_cache_is_shared_wherever_it_runs(self) -> None:
        with patch.dict("os.environ", {"VERCEL": "1"}, clear=False):
            store = RedisRestStore("https://redis.example", "secret")
            info = describe_store(store)
            store.close()
        self.assertEqual(info.kind, "redis")
        self.assertTrue(info.shared)

    def test_memory_never_survives_the_process(self) -> None:
        with patch.dict("os.environ", {**NO_REDIS, "IMPOSTER_MULTI_INSTANCE": "0"}, clear=False):
            info = describe_store(MemoryStore())
        self.assertEqual(info.kind, "memory")
        self.assertIn("lost when this process stops", info.detail)


class SessionTokenTests(unittest.TestCase):
    def test_a_made_up_token_is_rejected(self) -> None:
        hub = GameHub()
        room, host = hub.create_room("Host")
        code, player_id, _secret = host.token.split(".")
        with self.assertRaises(GameError) as caught:
            hub.resolve_token(f"{code}.{player_id}.guessed")
        self.assertEqual(caught.exception.code, "not_seated")
        self.assertEqual(caught.exception.status_code, 401)

    def test_a_token_from_an_older_release_is_rejected(self) -> None:
        hub = GameHub()
        hub.create_room("Host")
        with self.assertRaises(GameError) as caught:
            hub.resolve_token("plain-old-token")
        self.assertEqual(caught.exception.code, "session_invalid")

    def test_a_closed_room_is_told_apart_from_a_bad_session(self) -> None:
        hub = GameHub()
        _room, host = hub.create_room("Host")
        hub.store.delete(host.token.split(".")[0])
        with self.assertRaises(GameError) as caught:
            hub.resolve_token(host.token)
        self.assertEqual(caught.exception.code, "room_closed")
        self.assertEqual(caught.exception.status_code, 404)

    def test_polling_does_not_write_on_every_request(self) -> None:
        class CountingStore(MemoryStore):
            saves = 0

            def save(self, room: Room) -> None:
                type(self).saves += 1
                super().save(room)

        hub = GameHub(store=CountingStore())
        _room, host = hub.create_room("Host")
        writes = CountingStore.saves
        start = time.time()
        for step in range(20):
            hub.resolve_token(host.token, now=start + step)
        self.assertEqual(CountingStore.saves, writes)
        hub.resolve_token(host.token, now=start + 3600)
        self.assertEqual(CountingStore.saves, writes + 1)


if __name__ == "__main__":
    unittest.main()

import json
import time
import unittest
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

import store as store_module
from engine import (
    GameError,
    GameHub,
    MemoryStore,
    Room,
    StoreConflict,
    StoreUnavailable,
    room_from_dict,
    room_to_dict,
    serialized_room_fields,
)
from store import RedisRestStore, SqliteStore, create_store, room_ttl_seconds


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


class RoomSerializationTests(unittest.TestCase):
    def test_every_room_field_is_written(self) -> None:
        declared = {f.name for f in fields(Room)}
        self.assertEqual(declared, serialized_room_fields())

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

    def test_it_still_works_without_scripting(self) -> None:
        fake = FakeRedis(eval_supported=False)
        store = redis_store(fake)
        hub = GameHub(store=store)
        room, players = played_room(hub)
        reread = redis_store(fake).load(room.code)
        self.assertEqual(sorted(reread.players), sorted(room.players))
        self.assertEqual({parts[0] for parts in fake.commands} & {"EVAL"}, {"EVAL"})
        self.assertIn("SET", {parts[0] for parts in fake.commands})

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

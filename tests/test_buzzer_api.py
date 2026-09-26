import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from pathlib import Path

from fastapi.testclient import TestClient

import buzzer_api
import server
from buzzer import BUZZER_ROOMS, BuzzHub

PASSWORD = "quizmaster"


class BuzzerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        buzzer_api.hub = BuzzHub(rng=random.Random(0))
        buzzer_api.last_sweep = 0.0
        self.client = TestClient(server.app)

    # -- helpers ---------------------------------------------------------

    def open_room(self, password: str = PASSWORD) -> tuple[str, str]:
        made = self.client.post("/api/buzz/rooms", json={"password": password})
        self.assertEqual(made.status_code, 200, made.text)
        body = made.json()
        return body["token"], body["room"]["code"]

    def join(self, code: str, name: str) -> str:
        joined = self.client.post("/api/buzz/rooms/join", json={"code": code, "name": name})
        self.assertEqual(joined.status_code, 200, joined.text)
        return joined.json()["token"]

    def post(self, path: str, token: str, **kwargs):
        return self.client.post(path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

    def room(self, token: str, **params):
        return self.client.get(
            "/api/buzz/room", headers={"Authorization": f"Bearer {token}"}, params=params
        )

    # -- pages -----------------------------------------------------------

    def test_the_buzzer_has_its_own_page(self) -> None:
        for path in ("/buzzer", "/buzzer/KNTQ"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn("buzzer.js", page.text)

    def test_the_party_game_is_still_where_it_was(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Imposter", home.text)
        self.assertEqual(self.client.get("/join/KNTQ").status_code, 200)

    def test_the_buzzer_script_and_styles_are_served(self) -> None:
        for asset in ("/static/buzzer.js", "/static/buzzer.css"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)

    # -- getting in ------------------------------------------------------

    def test_hosting_needs_a_password_and_hands_back_a_console(self) -> None:
        short = self.client.post("/api/buzz/rooms", json={"password": "abc"})
        self.assertEqual(short.status_code, 400)
        self.assertIn("at least", short.json()["error"])

        token, code = self.open_room()
        self.assertEqual(len(code), 4)
        view = self.room(token).json()
        self.assertTrue(view["you"]["isHost"])
        self.assertEqual(view["phase"], "idle")
        self.assertTrue(view["joinQrSvg"].startswith("<svg"))
        self.assertTrue(view["joinUrl"].endswith(f"/buzzer/{code}"))

    def test_an_ipad_joins_with_the_code_and_a_name(self) -> None:
        _, code = self.open_room()
        token = self.join(code, "Ava")
        view = self.room(token).json()
        self.assertFalse(view["you"]["isHost"])
        self.assertEqual(view["you"]["name"], "Ava")
        self.assertNotIn("joinQrSvg", view)

    def test_a_player_cannot_reach_the_host_controls(self) -> None:
        host, code = self.open_room()
        player = self.join(code, "Ava")
        for path, payload in (
            ("/api/buzz/room/open", None),
            ("/api/buzz/room/next", None),
            ("/api/buzz/room/judge", {"correct": True}),
            ("/api/buzz/room/scores/reset", None),
        ):
            answer = self.post(path, player, json=payload)
            self.assertEqual(answer.status_code, 403, path)
            self.assertEqual(answer.json()["code"], "host_only")
        self.assertEqual(self.post("/api/buzz/room/open", host).status_code, 200)

    def test_the_console_needs_the_password_to_move_devices(self) -> None:
        host, code = self.open_room()
        wrong = self.client.post(
            "/api/buzz/rooms/host", json={"code": code, "password": "guessing"}
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.json()["code"], "bad_password")

        right = self.client.post(
            "/api/buzz/rooms/host", json={"code": code, "password": PASSWORD}
        )
        self.assertEqual(right.status_code, 200)
        self.assertTrue(right.json()["room"]["you"]["isHost"])
        # The iPad that was the console is signed out by the one that took it.
        self.assertEqual(self.room(host).status_code, 401)

    def test_an_unknown_room_says_so(self) -> None:
        answer = self.client.post("/api/buzz/rooms/join", json={"code": "ZZZZ", "name": "Ava"})
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()["code"], "room_closed")

    def test_meta_says_where_rooms_are_kept(self) -> None:
        meta = self.client.get("/api/buzz/meta").json()
        self.assertEqual(set(meta["roomStore"]), {"kind", "shared", "detail"})
        self.assertGreaterEqual(meta["minPasswordLength"], 6)

    # -- the button ------------------------------------------------------

    def test_the_host_opens_the_buzzers_and_the_first_tap_wins(self) -> None:
        host, code = self.open_room()
        ava = self.join(code, "Ava")
        ben = self.join(code, "Ben")

        early = self.post("/api/buzz/room/buzz", ava).json()
        self.assertFalse(early["tookIt"])
        self.assertEqual(early["reason"], "too_early")

        self.post("/api/buzz/room/open", host)
        took = self.post("/api/buzz/room/buzz", ben).json()
        self.assertTrue(took["tookIt"])
        self.assertEqual(took["room"]["winner"]["name"], "Ben")
        self.assertIsNotNone(took["room"]["winner"]["reactionMs"])

        missed = self.post("/api/buzz/room/buzz", ava).json()
        self.assertFalse(missed["tookIt"])
        self.assertEqual(missed["reason"], "too_late")
        self.assertIn("Ben", missed["message"])

        console = self.room(host).json()
        self.assertEqual(console["phase"], "answering")
        self.assertEqual(console["winner"]["name"], "Ben")

    def test_a_losing_tap_is_an_answer_rather_than_an_error(self) -> None:
        """The iPad gets the verdict and the new room in one round trip."""
        host, code = self.open_room()
        ava = self.join(code, "Ava")
        ben = self.join(code, "Ben")
        self.post("/api/buzz/room/open", host)
        self.post("/api/buzz/room/buzz", ava)

        answer = self.post("/api/buzz/room/buzz", ben)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["room"]["winner"]["name"], "Ava")

    def test_judging_moves_the_score_and_shuts_the_buzzers(self) -> None:
        host, code = self.open_room()
        ava = self.join(code, "Ava")
        self.post("/api/buzz/room/settings", host, json={"clueValue": 400})
        self.post("/api/buzz/room/open", host)
        self.post("/api/buzz/room/buzz", ava)

        judged = self.post("/api/buzz/room/judge", host, json={"correct": True}).json()
        self.assertEqual(judged["phase"], "idle")
        self.assertIsNone(judged["winner"])
        self.assertEqual(judged["players"][0]["score"], 400)
        self.assertEqual(judged["lastResult"]["name"], "Ava")
        self.assertEqual(judged["clueNumber"], 2)

    def test_a_wrong_answer_leaves_the_others_in(self) -> None:
        host, code = self.open_room()
        ava = self.join(code, "Ava")
        ben = self.join(code, "Ben")
        self.post("/api/buzz/room/open", host)
        self.post("/api/buzz/room/buzz", ava)
        self.post("/api/buzz/room/judge", host, json={"correct": False})
        self.post("/api/buzz/room/open", host)

        self.assertEqual(self.post("/api/buzz/room/buzz", ava).json()["reason"], "spent")
        self.assertTrue(self.post("/api/buzz/room/buzz", ben).json()["tookIt"])

    def test_the_host_can_fix_a_score_and_start_again(self) -> None:
        host, code = self.open_room()
        ava = self.join(code, "Ava")
        seat_id = self.room(ava).json()["you"]["id"]

        self.post("/api/buzz/room/score", host, json={"seatId": seat_id, "delta": -300})
        self.assertEqual(self.room(host).json()["players"][0]["score"], -300)
        self.post("/api/buzz/room/scores/reset", host)
        self.assertEqual(self.room(host).json()["players"][0]["score"], 0)

    def test_the_host_can_remove_an_ipad(self) -> None:
        host, code = self.open_room()
        ava = self.join(code, "Ava")
        seat_id = self.room(ava).json()["you"]["id"]
        self.post("/api/buzz/room/kick", host, json={"seatId": seat_id})

        self.assertEqual(self.room(host).json()["players"], [])
        self.assertEqual(self.room(ava).status_code, 401)

    def test_a_view_never_carries_the_password(self) -> None:
        host, code = self.open_room()
        self.join(code, "Ava")
        body = self.room(host).text
        self.assertNotIn(PASSWORD, body)
        self.assertNotIn("password", body.lower())


class RaceTests(unittest.TestCase):
    """Two iPads tapping at once must produce one winner, not two."""

    def setUp(self) -> None:
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        from store import SqliteStore

        store = SqliteStore(Path(self.dir.name) / "rooms.db", rooms=BUZZER_ROOMS)
        self.addCleanup(store.close)
        buzzer_api.hub = BuzzHub(rng=random.Random(0), store=store)
        buzzer_api.last_sweep = 0.0
        self.client = TestClient(server.app)

    def test_a_whole_room_buzzing_at_once_has_one_winner(self) -> None:
        made = self.client.post("/api/buzz/rooms", json={"password": PASSWORD}).json()
        host, code = made["token"], made["room"]["code"]
        tokens = [
            self.client.post(
                "/api/buzz/rooms/join", json={"code": code, "name": f"Player {n}"}
            ).json()["token"]
            for n in range(8)
        ]
        self.client.post("/api/buzz/room/open", headers={"Authorization": f"Bearer {host}"})

        start = threading.Barrier(len(tokens))

        def tap(token: str) -> dict:
            start.wait()
            return self.client.post(
                "/api/buzz/room/buzz", headers={"Authorization": f"Bearer {token}"}
            ).json()

        with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
            answers = list(pool.map(tap, tokens))

        winners = [answer for answer in answers if answer["tookIt"]]
        self.assertEqual(len(winners), 1)

        named = {answer["room"]["winner"]["name"] for answer in answers}
        self.assertEqual(len(named), 1, "every iPad has to agree on who got in")
        for answer in answers:
            if not answer["tookIt"]:
                self.assertEqual(answer["reason"], "too_late")


class HoldingTheLineTests(unittest.TestCase):
    """A device asks for the room and is answered when the room changes."""

    def setUp(self) -> None:
        buzzer_api.hub = BuzzHub(rng=random.Random(0))
        buzzer_api.last_sweep = 0.0
        self.client = TestClient(server.app)
        made = self.client.post("/api/buzz/rooms", json={"password": PASSWORD}).json()
        self.host, self.code = made["token"], made["room"]["code"]
        self.ava = self.client.post(
            "/api/buzz/rooms/join", json={"code": self.code, "name": "Ava"}
        ).json()["token"]

    def watch(self, token: str, since: str, wait: float = 5.0):
        return self.client.get(
            "/api/buzz/room",
            headers={"Authorization": f"Bearer {token}"},
            params={"since": since, "wait": wait},
        )

    def test_a_stale_revision_is_answered_at_once(self) -> None:
        answer = self.watch(self.ava, "a-revision-from-last-week", wait=5.0)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["phase"], "idle")

    def test_asking_without_a_revision_never_waits(self) -> None:
        before = time.monotonic()
        answer = self.client.get(
            "/api/buzz/room", headers={"Authorization": f"Bearer {self.ava}"}
        )
        self.assertEqual(answer.status_code, 200)
        self.assertLess(time.monotonic() - before, 1.0)

    def test_the_buzzers_opening_answers_a_waiting_ipad(self) -> None:
        rev = self.client.get(
            "/api/buzz/room", headers={"Authorization": f"Bearer {self.ava}"}
        ).json()["rev"]

        held: dict = {}

        def wait_for_it() -> None:
            started = time.monotonic()
            answer = self.watch(self.ava, rev, wait=10.0)
            held["took"] = time.monotonic() - started
            held["body"] = answer.json()

        watcher = threading.Thread(target=wait_for_it)
        watcher.start()
        time.sleep(0.4)
        self.client.post(
            "/api/buzz/room/open", headers={"Authorization": f"Bearer {self.host}"}
        )
        watcher.join(timeout=10)

        self.assertFalse(watcher.is_alive())
        self.assertEqual(held["body"]["phase"], "open")
        self.assertNotEqual(held["body"]["rev"], rev)
        # It came back because the buzzers opened, not because it gave up.
        self.assertLess(held["took"], 3.0)

    def test_a_hold_gives_up_politely_when_nothing_happens(self) -> None:
        rev = self.client.get(
            "/api/buzz/room", headers={"Authorization": f"Bearer {self.ava}"}
        ).json()["rev"]
        started = time.monotonic()
        answer = self.watch(self.ava, rev, wait=1.0)
        waited = time.monotonic() - started

        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["rev"], rev)
        self.assertGreaterEqual(waited, 0.9)
        self.assertLess(waited, 4.0)

    def test_every_ipad_in_the_room_is_answered_by_one_tap(self) -> None:
        names = ["Ben", "Cara", "Dev"]
        tokens = [self.ava] + [
            self.client.post(
                "/api/buzz/rooms/join", json={"code": self.code, "name": name}
            ).json()["token"]
            for name in names
        ]
        revs = {
            token: self.client.get(
                "/api/buzz/room", headers={"Authorization": f"Bearer {token}"}
            ).json()["rev"]
            for token in tokens
        }

        with ThreadPoolExecutor(max_workers=len(tokens) + 1) as pool:
            watching = [pool.submit(self.watch, token, revs[token], 10.0) for token in tokens]
            time.sleep(0.5)
            self.client.post(
                "/api/buzz/room/open", headers={"Authorization": f"Bearer {self.host}"}
            )
            answers = [held.result(timeout=12) for held in watching]

        for answer in answers:
            self.assertEqual(answer.status_code, 200)
            self.assertEqual(answer.json()["phase"], "open")

    def test_an_ipad_watching_a_room_that_closes_is_told(self) -> None:
        rev = self.client.get(
            "/api/buzz/room", headers={"Authorization": f"Bearer {self.ava}"}
        ).json()["rev"]
        self.client.post(
            "/api/buzz/room/kick",
            headers={"Authorization": f"Bearer {self.host}"},
            json={"seatId": self.client.get(
                "/api/buzz/room", headers={"Authorization": f"Bearer {self.ava}"}
            ).json()["you"]["id"]},
        )
        answer = self.watch(self.ava, rev, wait=2.0)
        self.assertEqual(answer.status_code, 401)


if __name__ == "__main__":
    unittest.main()

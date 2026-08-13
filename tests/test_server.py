import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import server
from engine import GameHub, StoreUnavailable
from store import SqliteStore


class ServerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        server.hub = GameHub()
        server.last_sweep = 0.0
        self.client = TestClient(server.app)

    def test_home_page_serves_ui(self) -> None:
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Imposter", res.text)

    def test_join_page_and_static_assets(self) -> None:
        join = self.client.get("/join/KNTQ")
        self.assertEqual(join.status_code, 200)
        self.assertIn("Imposter", join.text)
        css = self.client.get("/static/styles.css")
        self.assertEqual(css.status_code, 200)
        js = self.client.get("/static/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("function api(", js.text)

    def test_join_url_uses_forwarded_https_host(self) -> None:
        created = self.client.post(
            "/api/rooms",
            json={"name": "Host"},
            headers={
                "Host": "imposter.vercel.app",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "imposter.vercel.app",
            },
        )
        self.assertEqual(created.status_code, 200)
        join_url = created.json()["room"]["joinUrl"]
        self.assertTrue(join_url.startswith("https://imposter.vercel.app/join/"))

    def test_join_url_uses_public_origin_override(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"PUBLIC_ORIGIN": "https://play.example.com"}):
            created = self.client.post("/api/rooms", json={"name": "Host"})
        self.assertEqual(created.status_code, 200)
        self.assertTrue(
            created.json()["room"]["joinUrl"].startswith("https://play.example.com/join/")
        )

    def test_create_join_and_refuse_short_table(self) -> None:
        created = self.client.post("/api/rooms", json={"name": "Host"})
        self.assertEqual(created.status_code, 200)
        token = created.json()["token"]
        code = created.json()["room"]["code"]

        joined = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code})
        self.assertEqual(joined.status_code, 200)

        start = self.client.post("/api/room/start", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(start.status_code, 400)
        self.assertIn("3 players", start.json()["error"])

    def test_meta_includes_packs(self) -> None:
        res = self.client.get("/api/meta")
        self.assertEqual(res.status_code, 200)
        ids = {pack["id"] for pack in res.json()["packs"]}
        self.assertIn("party", ids)
        self.assertIn("house", ids)

    def test_create_room_includes_qr_and_join_url(self) -> None:
        created = self.client.post("/api/rooms", json={"name": "Host"})
        room = created.json()["room"]
        self.assertIn("/join/", room["joinUrl"])
        self.assertTrue(room["joinQrSvg"].lstrip().startswith("<svg"))
        self.assertEqual(room["code"] in room["joinUrl"], True)

    def test_phone_invite_and_claim(self) -> None:
        created = self.client.post("/api/rooms", json={"name": "Host"})
        token = created.json()["token"]
        invited = self.client.post(
            "/api/room/invite",
            json={"name": "Jordan", "phone": "5551234567"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(invited.status_code, 200)
        body = invited.json()
        self.assertIn("smsUrl", body)
        self.assertTrue(body["smsUrl"].startswith("sms:+15551234567"))
        invite_token = body["inviteToken"]
        peeked = self.client.get(f"/api/invites/{invite_token}")
        self.assertEqual(peeked.status_code, 200)
        self.assertEqual(peeked.json()["name"], "Jordan")
        joined = self.client.post(
            "/api/rooms/join",
            json={
                "name": "Jordan",
                "code": created.json()["room"]["code"],
                "inviteToken": invite_token,
            },
        )
        self.assertEqual(joined.status_code, 200)
        reused = self.client.get(f"/api/invites/{invite_token}")
        self.assertEqual(reused.status_code, 400)

    def test_full_round_via_api(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        ben = self.client.post("/api/rooms/join", json={"name": "Ben", "code": code}).json()
        auth = lambda token: {"Authorization": f"Bearer {token}"}

        packed = self.client.post(
            "/api/room/words/pack",
            json={"packId": "party"},
            headers=auth(host["token"]),
        )
        self.assertEqual(packed.status_code, 200)
        self.assertGreaterEqual(packed.json()["remainingWordCount"], 12)

        started = self.client.post("/api/room/start", headers=auth(host["token"]))
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["phase"], "reveal")
        self.assertIn(started.json()["you"]["role"]["kind"], {"imposter", "faithful"})

        guest = self.client.get("/api/room", headers=auth(ava["token"])).json()
        if guest["you"]["role"]["kind"] == "imposter":
            self.assertIsNone(guest["you"]["role"]["word"])
        else:
            self.assertTrue(guest["you"]["role"]["word"])

        for token in (host["token"], ava["token"], ben["token"]):
            self.client.post("/api/room/ready", headers=auth(token))
        discuss = self.client.get("/api/room", headers=auth(host["token"])).json()
        self.assertEqual(discuss["phase"], "discuss")
        self.assertIsNotNone(discuss["prompt"])
        self.assertEqual(discuss["prompt"], self.client.get("/api/room", headers=auth(ava["token"])).json()["prompt"])

        swapped = self.client.post("/api/room/next-prompt", headers=auth(host["token"]))
        self.assertEqual(swapped.status_code, 200)
        self.assertNotEqual(swapped.json()["prompt"]["id"], discuss["prompt"]["id"])

        self.client.post("/api/room/advance", headers=auth(host["token"]))
        huddle = self.client.get("/api/room", headers=auth(host["token"])).json()
        self.assertEqual(huddle["phase"], "huddle")

        self.client.post("/api/room/advance", headers=auth(host["token"]))
        guess = self.client.get("/api/room", headers=auth(host["token"])).json()
        self.assertEqual(guess["phase"], "guess")

        self.client.post("/api/room/advance", headers=auth(host["token"]))
        vote_state = self.client.get("/api/room", headers=auth(host["token"])).json()
        self.assertEqual(vote_state["phase"], "vote")
        target = next(p["id"] for p in vote_state["players"] if p["id"] != host["playerId"])
        voted = self.client.post(
            "/api/room/vote",
            json={"targetId": target},
            headers=auth(host["token"]),
        )
        self.assertEqual(voted.status_code, 200)

    def test_host_can_end_game_and_reopen(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        auth = lambda token: {"Authorization": f"Bearer {token}"}

        refused = self.client.post("/api/room/end", headers=auth(ava["token"]))
        self.assertEqual(refused.status_code, 403)

        ended = self.client.post("/api/room/end", headers=auth(host["token"]))
        self.assertEqual(ended.status_code, 200)
        self.assertEqual(ended.json()["phase"], "ended")

        guest = self.client.get("/api/room", headers=auth(ava["token"]))
        self.assertEqual(guest.status_code, 200)
        self.assertEqual(guest.json()["phase"], "ended")

        reopened = self.client.post("/api/room/reopen", headers=auth(host["token"]))
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json()["phase"], "lobby")

        left = self.client.post("/api/room/leave", headers=auth(ava["token"]))
        self.assertEqual(left.status_code, 200)

    def test_guest_can_add_a_word_in_lobby(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        added = self.client.post(
            "/api/room/words",
            json={"word": "Waffle"},
            headers={"Authorization": f"Bearer {ava['token']}"},
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.json()["remainingWordCount"], 1)
        packed = self.client.post(
            "/api/room/words/pack",
            json={"packId": "food"},
            headers={"Authorization": f"Bearer {ava['token']}"},
        )
        self.assertEqual(packed.status_code, 200)
        self.assertGreaterEqual(packed.json()["remainingWordCount"], 12)

    def test_host_can_disable_imposter_hints(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        self.assertTrue(host["room"]["imposterHints"])
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        self.client.post("/api/rooms/join", json={"name": "Ben", "code": code})
        auth = lambda token: {"Authorization": f"Bearer {token}"}
        self.client.post("/api/room/words", json={"word": "Toaster"}, headers=auth(host["token"]))
        updated = self.client.post(
            "/api/room/settings",
            json={"imposterHints": False},
            headers=auth(host["token"]),
        )
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()["imposterHints"])
        started = self.client.post("/api/room/start", headers=auth(host["token"]))
        self.assertEqual(started.status_code, 200)
        for token in (host["token"], ava["token"]):
            view = self.client.get("/api/room", headers=auth(token)).json()
            self.assertFalse(view["imposterHints"])
            if view["you"]["role"]["kind"] == "imposter":
                self.assertIsNone(view["you"]["role"]["clue"])
            else:
                self.assertEqual(view["you"]["role"]["word"], "Toaster")

    def test_same_name_rejoins_after_session_drop(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        old_token = ava["token"]
        player_id = ava["playerId"]
        again = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["playerId"], player_id)
        self.assertNotEqual(again.json()["token"], old_token)
        stale = self.client.get("/api/room", headers={"Authorization": f"Bearer {old_token}"})
        self.assertEqual(stale.status_code, 401)
        fresh = self.client.get(
            "/api/room",
            headers={"Authorization": f"Bearer {again.json()['token']}"},
        )
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(fresh.json()["you"]["name"], "Ava")
        self.assertEqual(fresh.json()["code"], code)


class ReconnectTests(unittest.TestCase):
    """The reported bug: a phone that waits too long is told its session died."""

    def setUp(self) -> None:
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "rooms.db"
        self.restart()
        self.client = TestClient(server.app)

    def restart(self) -> None:
        """Stand in for a redeploy, a cold start, or a second instance."""
        store = SqliteStore(self.db)
        self.addCleanup(store.close)
        server.hub = GameHub(store=store)
        server.last_sweep = 0.0

    def test_a_phone_picks_its_seat_back_up_after_a_restart(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        auth = {"Authorization": f"Bearer {host['token']}"}
        self.client.post("/api/room/words", json={"word": "Toaster"}, headers=auth)

        self.restart()

        back = self.client.get("/api/room", headers=auth)
        self.assertEqual(back.status_code, 200)
        self.assertEqual(back.json()["code"], host["room"]["code"])
        self.assertEqual(back.json()["remainingWordCount"], 1)
        self.assertTrue(back.json()["you"]["isHost"])

    def test_a_guest_who_takes_their_time_still_gets_in(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]

        self.restart()

        joined = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code})
        self.assertEqual(joined.status_code, 200)
        seen = self.client.get(
            "/api/room", headers={"Authorization": f"Bearer {host['token']}"}
        ).json()
        self.assertEqual(sorted(p["name"] for p in seen["players"]), ["Ava", "Host"])

    def test_a_texted_invite_outlives_a_restart(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        auth = {"Authorization": f"Bearer {host['token']}"}
        invited = self.client.post(
            "/api/room/invite",
            json={"name": "Jordan", "phone": "5551234567"},
            headers=auth,
        ).json()

        self.restart()

        peeked = self.client.get(f"/api/invites/{invited['inviteToken']}")
        self.assertEqual(peeked.status_code, 200)
        self.assertEqual(peeked.json()["name"], "Jordan")

    def test_a_round_in_progress_is_still_there(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        self.client.post("/api/rooms/join", json={"name": "Ben", "code": code})
        auth = {"Authorization": f"Bearer {host['token']}"}
        self.client.post("/api/room/words/pack", json={"packId": "party"}, headers=auth)
        started = self.client.post("/api/room/start", headers=auth).json()

        self.restart()

        guest = self.client.get(
            "/api/room", headers={"Authorization": f"Bearer {ava['token']}"}
        ).json()
        self.assertEqual(guest["phase"], "reveal")
        self.assertEqual(guest["roundNumber"], started["roundNumber"])
        self.assertIn(guest["you"]["role"]["kind"], {"imposter", "faithful"})

    def test_a_missed_guess_is_still_missed_after_a_restart(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        ben = self.client.post("/api/rooms/join", json={"name": "Ben", "code": code}).json()
        auth = lambda token: {"Authorization": f"Bearer {token}"}
        self.client.post(
            "/api/room/settings", json={"numImposters": 2}, headers=auth(host["token"])
        )
        self.client.post(
            "/api/room/words/pack", json={"packId": "party"}, headers=auth(host["token"])
        )
        self.client.post("/api/room/start", headers=auth(host["token"]))
        for _ in range(3):
            self.client.post("/api/room/advance", headers=auth(host["token"]))
        seats = {host["playerId"]: host, ava["playerId"]: ava, ben["playerId"]: ben}
        guessing = self.client.get("/api/room", headers=auth(host["token"])).json()
        self.assertEqual(guessing["phase"], "guess")
        imposter = next(
            seats[p["id"]]
            for p in guessing["players"]
            if self.client.get("/api/room", headers=auth(seats[p["id"]]["token"])).json()["you"][
                "canGuess"
            ]
        )
        missed = self.client.post(
            "/api/room/guess", json={"word": "not it"}, headers=auth(imposter["token"])
        )
        self.assertEqual(missed.status_code, 200)

        self.restart()

        back = self.client.get("/api/room", headers=auth(imposter["token"])).json()
        self.assertEqual(back["phase"], "guess")
        self.assertTrue(back["you"]["hasGuessed"])
        self.assertFalse(back["you"]["canGuess"])
        spent = self.client.post(
            "/api/room/guess", json={"word": "second try"}, headers=auth(imposter["token"])
        )
        self.assertEqual(spent.status_code, 400)

    def test_a_lost_phone_sits_back_down_under_the_same_name(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        code = host["room"]["code"]
        ava = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()

        self.restart()

        again = self.client.post("/api/rooms/join", json={"name": "Ava", "code": code}).json()
        self.assertEqual(again["playerId"], ava["playerId"])
        self.assertNotEqual(again["token"], ava["token"])
        stale = self.client.get("/api/room", headers={"Authorization": f"Bearer {ava['token']}"})
        self.assertEqual(stale.status_code, 401)
        self.assertEqual(stale.json()["code"], "not_seated")
        fresh = self.client.get(
            "/api/room", headers={"Authorization": f"Bearer {again['token']}"}
        )
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(fresh.json()["you"]["name"], "Ava")

    def test_an_unreachable_store_is_worth_retrying(self) -> None:
        host = self.client.post("/api/rooms", json={"name": "Host"}).json()

        class DownStore(SqliteStore):
            def load(self, code: str):
                raise StoreUnavailable("cache is down")

        store = DownStore(self.db)
        self.addCleanup(store.close)
        server.hub = GameHub(store=store)

        res = self.client.get(
            "/api/room", headers={"Authorization": f"Bearer {host['token']}"}
        )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "store_unavailable")

    def test_errors_name_themselves_so_a_phone_knows_when_to_wait(self) -> None:
        missing = self.client.get("/api/room")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["code"], "no_session")

        garbled = self.client.get("/api/room", headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(garbled.status_code, 401)
        self.assertEqual(garbled.json()["code"], "session_invalid")

        host = self.client.post("/api/rooms", json={"name": "Host"}).json()
        server.hub.store.delete(host["room"]["code"])
        gone = self.client.get(
            "/api/room", headers={"Authorization": f"Bearer {host['token']}"}
        )
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(gone.json()["code"], "room_closed")


if __name__ == "__main__":
    unittest.main()

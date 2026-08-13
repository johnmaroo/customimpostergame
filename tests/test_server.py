import unittest

from fastapi.testclient import TestClient

import server
from engine import GameHub


class ServerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        server.hub = GameHub()
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
        vote_state = self.client.get("/api/room", headers=auth(host["token"])).json()
        self.assertEqual(vote_state["phase"], "vote")
        target = next(p["id"] for p in vote_state["players"] if p["id"] != host["playerId"])
        voted = self.client.post(
            "/api/room/vote",
            json={"targetId": target},
            headers=auth(host["token"]),
        )
        self.assertEqual(voted.status_code, 200)

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


if __name__ == "__main__":
    unittest.main()

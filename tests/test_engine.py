import json
import random
import time
import unittest

from engine import GameError, GameHub, MIN_PLAYERS


class GameHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = GameHub(rng=random.Random(0))

    def _table(self, words: list[str] | None = None):
        room, host = self.hub.create_room("Host")
        _, ava = self.hub.join_room(room.code, "Ava")
        _, ben = self.hub.join_room(room.code, "Ben")
        for word in words or ["Toaster", "Sofa"]:
            self.hub.add_word(room, host, word)
        return room, host, ava, ben

    def test_create_and_join_assigns_host(self) -> None:
        room, host = self.hub.create_room("  Maya  ")
        self.assertEqual(host.name, "Maya")
        self.assertTrue(host.is_host)
        _, other = self.hub.join_room(room.code.lower(), "Jordan")
        self.assertFalse(other.is_host)
        self.assertEqual(len(room.players), 2)

    def test_duplicate_names_rejected(self) -> None:
        room, _host = self.hub.create_room("Maya")
        with self.assertRaises(GameError):
            self.hub.join_room(room.code, "maya")

    def test_unknown_room(self) -> None:
        with self.assertRaises(GameError) as ctx:
            self.hub.join_room("ZZZZ", "Ava")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cannot_start_with_too_few_players(self) -> None:
        room, host = self.hub.create_room("Host")
        self.hub.add_word(room, host, "Toaster")
        with self.assertRaises(GameError):
            self.hub.start_round(room, host)
        self.assertGreaterEqual(MIN_PLAYERS, 3)

    def test_start_round_hides_word_from_imposters(self) -> None:
        room, host, ava, ben = self._table(["Telescope"])
        rnd = self.hub.start_round(room, host, clue="things for looking far away")
        self.assertEqual(room.phase, "reveal")
        self.assertEqual(rnd.word, "Telescope")
        self.assertEqual(room.remaining_words, [])
        self.assertEqual(room.used_words, ["Telescope"])

        views = {p.id: self.hub.view_for(room, p) for p in (host, ava, ben)}
        imposters = set(rnd.imposter_ids)
        self.assertEqual(len(imposters), 1)
        for player in (host, ava, ben):
            view = views[player.id]
            blob = json.dumps(view)
            self.assertNotIn("imposterIds", blob)
            self.assertNotIn(json.dumps(rnd.imposter_ids), blob)
            if player.id in imposters:
                self.assertEqual(view["you"]["role"]["kind"], "imposter")
                self.assertIsNone(view["you"]["role"]["word"])
                self.assertNotIn("Telescope", blob)
                self.assertEqual(view["you"]["role"]["clue"], "things for looking far away")
            else:
                self.assertEqual(view["you"]["role"]["kind"], "faithful")
                self.assertEqual(view["you"]["role"]["word"], "Telescope")

    def test_ready_all_players_enters_discuss(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        self.hub.set_settings(room, host, discuss_seconds=60)
        self.hub.start_round(room, host)
        for player in (host, ava, ben):
            self.hub.mark_ready(room, player)
        self.assertEqual(room.phase, "discuss")
        self.assertIsNotNone(room.round.discuss_ends_at)
        self.assertAlmostEqual(room.round.discuss_ends_at, time.time() + 60, delta=2)

    def test_timer_moves_to_vote(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        self.hub.set_settings(room, host, discuss_seconds=45)
        self.hub.start_round(room, host, now=0)
        self.hub.advance(room, host, now=0)
        self.assertEqual(room.phase, "discuss")
        self.hub.tick(room, now=44)
        self.assertEqual(room.phase, "discuss")
        self.hub.tick(room, now=45)
        self.assertEqual(room.phase, "vote")

    def test_cannot_vote_for_self(self) -> None:
        room, host, ava, _ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        self.hub.advance(room, host)
        self.hub.advance(room, host)
        self.assertEqual(room.phase, "vote")
        with self.assertRaises(GameError):
            self.hub.vote(room, ava, ava.id)

    def test_faithfuls_win_when_imposter_is_named(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        rnd = self.hub.start_round(room, host)
        imposter_id = rnd.imposter_ids[0]
        self.hub.advance(room, host)
        self.hub.advance(room, host)
        for player in (host, ava, ben):
            target = None if player.id == imposter_id else imposter_id
            if target == player.id:
                continue
            self.hub.vote(room, player, target)
        self.assertEqual(room.phase, "results")
        self.assertEqual(room.round.winner, "faithfuls")
        for player in (host, ava, ben):
            if player.id == imposter_id:
                self.assertEqual(player.score, 0)
            else:
                self.assertEqual(player.score, 3)

    def test_imposters_win_on_a_tie(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        rnd = self.hub.start_round(room, host)
        self.hub.advance(room, host)
        self.hub.advance(room, host)
        imposter = next(p for p in (host, ava, ben) if p.id in rnd.imposter_ids)
        faithfuls = [p for p in (host, ava, ben) if p.id not in rnd.imposter_ids]
        self.hub.vote(room, faithfuls[0], faithfuls[1].id)
        self.hub.vote(room, faithfuls[1], faithfuls[0].id)
        self.hub.vote(room, imposter, None)
        self.assertEqual(room.phase, "results")
        self.assertEqual(room.round.winner, "imposters")
        self.assertEqual(imposter.score, 3)

    def test_late_joiner_sits_out(self) -> None:
        room, host, _ava, _ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        _, casey = self.hub.join_room(room.code, "Casey")
        view = self.hub.view_for(room, casey)
        self.assertTrue(view["you"]["sittingOut"])
        self.assertIsNone(view["you"].get("role"))

    def test_host_leave_transfers_host(self) -> None:
        room, host, ava, _ben = self._table()
        self.hub.leave(room, host)
        self.assertNotIn(host.id, room.players)
        self.assertTrue(ava.is_host)

    def test_recycle_words(self) -> None:
        room, host, _ava, _ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        self.hub.advance(room, host)
        self.hub.advance(room, host)
        self.hub.advance(room, host)
        self.assertEqual(room.phase, "results")
        with self.assertRaises(GameError):
            self.hub.start_round(room, host)
        restored = self.hub.recycle_words(room, host)
        self.assertEqual(restored, 1)
        self.assertEqual(room.remaining_words, ["Toaster"])

    def test_non_host_cannot_start(self) -> None:
        room, _host, ava, _ben = self._table()
        with self.assertRaises(GameError) as ctx:
            self.hub.start_round(room, ava)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()

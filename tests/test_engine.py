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

    def _advance_to(self, room, host, phase: str, now: float | None = None) -> None:
        for _ in range(8):
            if room.phase == phase:
                return
            self.hub.advance(room, host, now=now)
        self.fail(f"expected phase {phase}, stuck at {room.phase}")

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
                self.assertIsNone(view["you"]["role"]["clue"])
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
        self.assertIsNone(room.round.discuss_ends_at)

    def test_open_floor_timer_moves_to_guess(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        self.hub.set_settings(room, host, discuss_seconds=45)
        self.hub.start_round(room, host, now=0)
        self.hub.advance(room, host, now=0)
        self.assertEqual(room.phase, "discuss")
        self.hub.advance(room, host, now=0)
        self.assertEqual(room.phase, "huddle")
        self.hub.tick(room, now=44)
        self.assertEqual(room.phase, "huddle")
        self.hub.tick(room, now=45)
        self.assertEqual(room.phase, "guess")

    def test_cannot_vote_for_self(self) -> None:
        room, host, ava, _ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        self._advance_to(room, host, "vote")
        with self.assertRaises(GameError):
            self.hub.vote(room, ava, ava.id)

    def test_faithfuls_win_when_imposter_is_named(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        rnd = self.hub.start_round(room, host)
        imposter_id = rnd.imposter_ids[0]
        self._advance_to(room, host, "vote")
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
        self._advance_to(room, host, "vote")
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
        self._advance_to(room, host, "results")
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

    def test_phone_invite_then_join_with_token(self) -> None:
        room, host = self.hub.create_room("Host")
        invite = self.hub.add_invite(room, host, "Jordan", "+15551234567")
        looked, found = self.hub.lookup_invite(invite.token)
        self.assertEqual(looked.code, room.code)
        self.assertEqual(found.name, "Jordan")
        _, jordan = self.hub.join_room(room.code, "Jordan", invite_token=invite.token)
        self.assertEqual(jordan.phone, "+15551234567")
        self.assertTrue(invite.claimed_by)
        view = self.hub.view_for(room, host)
        self.assertTrue(view["invites"][0]["claimed"])
        with self.assertRaises(GameError):
            self.hub.lookup_invite(invite.token)

    def test_duplicate_invite_phone_rejected(self) -> None:
        room, host = self.hub.create_room("Host")
        self.hub.add_invite(room, host, "Ava", "+15550001111")
        with self.assertRaises(GameError):
            self.hub.add_invite(room, host, "Ben", "+15550001111")

    def test_host_can_end_from_lobby_and_reopen(self) -> None:
        room, host, ava, _ben = self._table()
        self.hub.end_game(room, host)
        self.assertEqual(room.phase, "ended")
        view = self.hub.view_for(room, ava)
        self.assertEqual(view["phase"], "ended")
        self.assertIsNone(view.get("result"))
        with self.assertRaises(GameError) as ctx:
            self.hub.start_round(room, host)
        self.assertIn("ended", ctx.exception.message.lower())
        self.hub.reopen_lobby(room, host)
        self.assertEqual(room.phase, "lobby")
        self.assertIsNone(room.round)
        self.assertIn(ava.id, room.players)

    def test_host_can_end_mid_round_and_reveal_word(self) -> None:
        room, host, ava, ben = self._table(["Telescope"])
        rnd = self.hub.start_round(room, host)
        self.hub.end_game(room, host)
        self.assertEqual(room.phase, "ended")
        view = self.hub.view_for(room, ava)
        self.assertEqual(view["result"]["word"], "Telescope")
        self.assertEqual(set(view["result"]["imposterIds"]), set(rnd.imposter_ids))
        self.hub.leave(room, ben)
        self.assertNotIn(ben.id, room.players)

    def test_non_host_cannot_end_or_reopen(self) -> None:
        room, host, ava, _ben = self._table()
        with self.assertRaises(GameError) as ctx:
            self.hub.end_game(room, ava)
        self.assertEqual(ctx.exception.status_code, 403)
        self.hub.end_game(room, host)
        with self.assertRaises(GameError) as ctx:
            self.hub.reopen_lobby(room, ava)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_go_around_again_starts_a_new_lap(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        self._advance_to(room, host, "discuss")
        first = list(room.round.speaking_order)
        self.hub.next_speaker(room, host)
        self.hub.next_speaker(room, host)
        self.assertEqual(room.round.speaker_index, 2)
        self.hub.go_around_again(room, host)
        self.assertEqual(room.phase, "discuss")
        self.assertEqual(room.round.lap, 2)
        self.assertEqual(room.round.speaker_index, 0)
        self.assertEqual(room.round.speaking_order, first[1:] + first[:1])

    def test_imposter_guesses_the_word_and_wins(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        rnd = self.hub.start_round(room, host)
        imposter = next(p for p in (host, ava, ben) if p.id in rnd.imposter_ids)
        faithful = next(p for p in (host, ava, ben) if p.id not in rnd.imposter_ids)
        self._advance_to(room, host, "guess")
        view = self.hub.view_for(room, imposter)
        self.assertTrue(view["you"]["canGuess"])
        self.assertIsNone(view["you"]["role"]["clue"])
        self.assertNotIn("Toaster", json.dumps(view))
        with self.assertRaises(GameError):
            self.hub.guess_word(room, faithful, "Toaster")
        self.hub.guess_word(room, imposter, " toaster! ")
        self.assertEqual(room.phase, "results")
        self.assertEqual(room.round.winner, "imposters")
        self.assertEqual(room.round.win_reason, "guess")
        self.assertEqual(imposter.score, 4)
        self.assertEqual(faithful.score, 0)
        result = self.hub.view_for(room, faithful)["result"]
        self.assertEqual(result["guessedBy"], imposter.id)
        self.assertEqual(result["word"], "Toaster")

    def test_wrong_guess_unlocks_clue_then_votes(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        rnd = self.hub.start_round(room, host, clue="kitchen appliances")
        imposter = next(p for p in (host, ava, ben) if p.id in rnd.imposter_ids)
        self._advance_to(room, host, "guess")
        self.hub.guess_word(room, imposter, "Sofa")
        self.assertEqual(room.phase, "vote")
        view = self.hub.view_for(room, imposter)
        self.assertTrue(view["you"]["guessMissed"])
        self.assertEqual(view["you"]["role"]["clue"], "kitchen appliances")
        self.assertNotIn("Toaster", json.dumps({"role": view["you"]["role"]}))

    def test_pass_and_play_skips_private_guess(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        self.hub.set_settings(room, host, pass_and_play=True)
        rnd = self.hub.start_round(room, host, clue="kitchen appliances")
        imposter = next(p for p in (host, ava, ben) if p.id in rnd.imposter_ids)
        self.assertEqual(self.hub.view_for(room, imposter)["you"]["role"]["clue"], "kitchen appliances")
        self._advance_to(room, host, "huddle")
        self.hub.advance(room, host)
        self.assertEqual(room.phase, "vote")


if __name__ == "__main__":
    unittest.main()

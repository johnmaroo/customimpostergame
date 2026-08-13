import json
import random
import time
import unittest

from engine import (
    GameError,
    GameHub,
    MIN_PLAYERS,
    _deal_speaking_order,
    _sample_by_fewest,
)


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


class DealHelperTests(unittest.TestCase):
    def test_fewest_skips_recent_when_everyone_is_tied(self) -> None:
        rng = random.Random(0)
        counts = {"a": 1, "b": 1, "c": 1}
        picked = [
            _sample_by_fewest(rng, ["a", "b", "c"], counts, 1, avoid=["a"])[0]
            for _ in range(24)
        ]
        self.assertNotIn("a", picked)
        self.assertEqual(set(picked), {"b", "c"})

    def test_fewest_still_picks_whoever_is_behind(self) -> None:
        rng = random.Random(0)
        counts = {"a": 0, "b": 1, "c": 1}
        choice = _sample_by_fewest(rng, ["a", "b", "c"], counts, 1, avoid=["a"])[0]
        self.assertEqual(choice, "a")

    def test_speaking_order_changes_starter_and_guest_tail(self) -> None:
        rng = random.Random(1)
        ids = ["h", "a", "b", "c"]
        counts: dict[str, int] = {}
        first = _deal_speaking_order(rng, ids, counts, previous=ids, last_starter=None)
        counts[first[0]] = 1
        second = _deal_speaking_order(
            rng, ids, counts, previous=first, last_starter=first[0]
        )
        self.assertEqual(set(first), set(ids))
        self.assertEqual(set(second), set(ids))
        self.assertNotEqual(first, ids)
        self.assertNotEqual(second[0], first[0])
        self.assertNotEqual(first, second)


class RoundDealTests(unittest.TestCase):
    def _table(self, hub: GameHub, names: list[str], words: list[str]):
        room, host = hub.create_room(names[0])
        players = [host]
        for name in names[1:]:
            _, player = hub.join_room(room.code, name)
            players.append(player)
        for word in words:
            hub.add_word(room, host, word)
        return room, host, players

    def _finish(self, hub: GameHub, room, host) -> None:
        while room.phase not in ("results", "lobby"):
            hub.advance(room, host)

    def test_imposters_cycle_before_anyone_repeats(self) -> None:
        for seed in range(20):
            hub = GameHub(rng=random.Random(seed))
            room, host, players = self._table(
                hub, ["Host", "Ava", "Ben"], ["one", "two", "three"]
            )
            seen: list[str] = []
            for _ in range(3):
                rnd = hub.start_round(room, host)
                self.assertEqual(len(rnd.imposter_ids), 1)
                seen.append(rnd.imposter_ids[0])
                self._finish(hub, room, host)
            self.assertEqual(len(set(seen)), 3)
            self.assertEqual(set(seen), {p.id for p in players})

    def test_starter_cycles_before_anyone_repeats(self) -> None:
        for seed in range(20):
            hub = GameHub(rng=random.Random(seed))
            room, host, players = self._table(
                hub, ["Host", "Ava", "Ben"], ["one", "two", "three"]
            )
            starters: list[str] = []
            for _ in range(3):
                rnd = hub.start_round(room, host)
                starters.append(rnd.speaking_order[0])
                self._finish(hub, room, host)
            self.assertEqual(len(set(starters)), 3)
            self.assertEqual(set(starters), {p.id for p in players})

    def test_speaking_order_is_a_fresh_permutation_each_round(self) -> None:
        for seed in range(20):
            hub = GameHub(rng=random.Random(seed))
            room, host, players = self._table(
                hub, ["Host", "Ava", "Ben", "Cara"], ["one", "two"]
            )
            join_ids = [p.id for p in players]
            first = hub.start_round(room, host)
            self.assertEqual(set(first.speaking_order), set(join_ids))
            self.assertNotEqual(first.speaking_order, join_ids)
            self._finish(hub, room, host)
            second = hub.start_round(room, host)
            self.assertEqual(set(second.speaking_order), set(join_ids))
            self.assertNotEqual(second.speaking_order, first.speaking_order)
            self.assertNotEqual(second.speaking_order[0], first.speaking_order[0])

    def test_two_imposters_deal_the_other_pair_next(self) -> None:
        for seed in range(15):
            hub = GameHub(rng=random.Random(seed))
            room, host, players = self._table(
                hub, ["Host", "Ava", "Ben", "Cara"], ["one", "two"]
            )
            hub.set_settings(room, host, num_imposters=2)
            first = hub.start_round(room, host)
            self._finish(hub, room, host)
            second = hub.start_round(room, host)
            self.assertEqual(set(first.imposter_ids) & set(second.imposter_ids), set())
            self.assertEqual(
                set(first.imposter_ids) | set(second.imposter_ids),
                {p.id for p in players},
            )

    def test_new_player_is_due_as_imposter(self) -> None:
        hub = GameHub(rng=random.Random(0))
        room, host, _players = self._table(
            hub, ["Host", "Ava", "Ben"], ["one", "two", "three", "four"]
        )
        for _ in range(3):
            hub.start_round(room, host)
            self._finish(hub, room, host)
        _, dana = hub.join_room(room.code, "Dana")
        rnd = hub.start_round(room, host)
        self.assertEqual(rnd.imposter_ids, [dana.id])


if __name__ == "__main__":
    unittest.main()

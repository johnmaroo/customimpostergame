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

    def test_same_name_rejoins_existing_seat(self) -> None:
        room, host = self.hub.create_room("Maya")
        old_token = host.token
        host_id = host.id
        room2, again = self.hub.join_room(room.code, "maya")
        self.assertIs(room2, room)
        self.assertEqual(again.id, host_id)
        self.assertTrue(again.is_host)
        self.assertNotEqual(again.token, old_token)
        self.assertEqual(len(room.players), 1)
        with self.assertRaises(GameError) as ctx:
            self.hub.resolve_token(old_token)
        self.assertEqual(ctx.exception.status_code, 401)
        room3, player = self.hub.resolve_token(again.token)
        self.assertEqual(player.id, host_id)
        self.assertEqual(room3.code, room.code)

    def test_rejoin_mid_round_keeps_role_and_score(self) -> None:
        room, host, ava, ben = self._table(["Telescope"])
        rnd = self.hub.start_round(room, host, clue="things for looking far away")
        ava.score = 5
        old_id = ava.id
        _, back = self.hub.join_room(room.code, "Ava")
        self.assertEqual(back.id, old_id)
        self.assertEqual(back.score, 5)
        view = self.hub.view_for(room, back)
        self.assertEqual(view["you"]["id"], old_id)
        if back.id in rnd.imposter_ids:
            self.assertEqual(view["you"]["role"]["kind"], "imposter")
        else:
            self.assertEqual(view["you"]["role"]["word"], "Telescope")

    def test_claimed_invite_rejoins_that_player(self) -> None:
        room, host = self.hub.create_room("Host")
        invite = self.hub.add_invite(room, host, "Jordan", "+15551234567")
        _, jordan = self.hub.join_room(room.code, "Jordan", invite_token=invite.token)
        old_token = jordan.token
        _, again = self.hub.join_room(room.code, "Someone", invite_token=invite.token)
        self.assertEqual(again.id, jordan.id)
        self.assertEqual(again.phone, "+15551234567")
        self.assertNotEqual(again.token, old_token)

    def test_rooms_survive_a_new_hub_from_disk(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rooms.json"
            hub = GameHub(rng=random.Random(0), persist_path=path)
            room, host = hub.create_room("Host")
            hub.join_room(room.code, "Ava")
            hub.add_word(room, host, "Toaster")
            code = room.code
            token = host.token
            restored = GameHub(rng=random.Random(1), persist_path=path)
            self.assertIn(code, restored.rooms)
            again, player = restored.resolve_token(token)
            self.assertEqual(player.name, "Host")
            self.assertEqual(again.remaining_words, ["Toaster"])
            self.assertEqual(len(again.players), 2)

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

    def test_round_deals_shared_irl_prompt(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        rnd = self.hub.start_round(room, host)
        self.assertIsNotNone(rnd.prompt)
        self.assertIn(rnd.prompt["kind"], {"ask", "do"})
        views = [self.hub.view_for(room, p) for p in (host, ava, ben)]
        texts = {view["prompt"]["text"] for view in views}
        self.assertEqual(len(texts), 1)
        self.assertEqual(views[0]["irlMode"], "mix")
        for view in views:
            blob = json.dumps(view)
            if view["you"]["role"]["kind"] == "imposter":
                self.assertNotIn("Toaster", blob)

    def test_irl_off_skips_prompt(self) -> None:
        room, host, _ava, _ben = self._table(["Toaster"])
        self.hub.set_settings(room, host, irl_mode="off")
        rnd = self.hub.start_round(room, host)
        self.assertIsNone(rnd.prompt)
        self.assertIsNone(self.hub.view_for(room, host)["prompt"])

    def test_ask_mode_deals_a_question(self) -> None:
        room, host, _ava, _ben = self._table(["Toaster"])
        self.hub.set_settings(room, host, irl_mode="ask")
        rnd = self.hub.start_round(room, host)
        self.assertEqual(rnd.prompt["kind"], "ask")

    def test_host_can_swap_prompt_during_discuss(self) -> None:
        room, host, ava, ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        first = room.round.prompt["id"]
        for player in (host, ava, ben):
            self.hub.mark_ready(room, player)
        swapped = self.hub.next_prompt(room, host)
        self.assertIsNotNone(swapped)
        self.assertNotEqual(swapped["id"], first)
        self.assertEqual(room.round.prompt["id"], swapped["id"])

    def test_guest_can_add_words_in_lobby(self) -> None:
        room, _host, ava, _ben = self._table(["Toaster"])
        added = self.hub.add_word(room, ava, "Waffle")
        self.assertEqual(added, "Waffle")
        self.assertIn("Waffle", room.remaining_words)

    def test_guest_cannot_add_words_during_a_round(self) -> None:
        room, host, ava, _ben = self._table(["Toaster"])
        self.hub.start_round(room, host)
        with self.assertRaises(GameError):
            self.hub.add_word(room, ava, "Waffle")

    def test_guest_cannot_load_saved_words(self) -> None:
        room, _host, ava, _ben = self._table(["Toaster"])
        with self.assertRaises(GameError) as ctx:
            self.hub.add_words(room, ava, ["Secret"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_invalid_irl_mode_rejected(self) -> None:
        room, host, _ava, _ben = self._table()
        with self.assertRaises(GameError):
            self.hub.set_settings(room, host, irl_mode="dares")

    def test_imposter_hints_can_be_turned_off(self) -> None:
        room, host, ava, ben = self._table(["Telescope"])
        self.assertTrue(room.imposter_hints)
        self.hub.set_settings(room, host, imposter_hints=False)
        self.assertFalse(room.imposter_hints)
        rnd = self.hub.start_round(room, host, clue="things for looking far away")
        imposters = set(rnd.imposter_ids)
        for player in (host, ava, ben):
            view = self.hub.view_for(room, player)
            blob = json.dumps(view)
            self.assertFalse(view["imposterHints"])
            if player.id in imposters:
                self.assertEqual(view["you"]["role"]["kind"], "imposter")
                self.assertIsNone(view["you"]["role"]["clue"])
                self.assertNotIn("Telescope", blob)
                self.assertNotIn("looking far away", blob)
            else:
                self.assertEqual(view["you"]["role"]["word"], "Telescope")


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

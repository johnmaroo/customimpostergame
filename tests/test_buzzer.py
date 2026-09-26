import json
import random
import time
import unittest
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory

import buzzer
from buzzer import (
    DEFAULT_FALSE_START_MS,
    MAX_PLAYERS,
    BuzzHub,
    BuzzRoom,
    Seat,
    room_from_dict,
    room_to_dict,
    serialized_room_fields,
    serialized_seat_fields,
    state_revision,
)
from errors import GameError

PASSWORD = "quizmaster"


def room_with(hub: BuzzHub, *names: str) -> tuple[BuzzRoom, Seat, list[Seat]]:
    room, host = hub.create_room(PASSWORD)
    players = [hub.join_room(room.code, name)[1] for name in names]
    return room, host, players


class GettingInTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))

    def test_a_room_needs_a_password_worth_having(self) -> None:
        with self.assertRaises(GameError) as caught:
            self.hub.create_room("abc")
        self.assertIn("at least", caught.exception.message)

    def test_the_password_is_never_written_down_in_the_clear(self) -> None:
        room, _ = self.hub.create_room(PASSWORD)
        blob = json.dumps(room_to_dict(room))
        self.assertNotIn(PASSWORD, blob)
        self.assertTrue(room.password_hash)

    def test_the_host_console_moves_to_whoever_knows_the_password(self) -> None:
        room, first = self.hub.create_room(PASSWORD)
        opened_with = first.token
        _, second = self.hub.claim_host(room.code, PASSWORD)

        self.assertEqual(second.id, first.id)
        self.assertNotEqual(second.token, opened_with)
        seen_room, seen = self.hub.resolve_token(second.token)
        self.assertTrue(seen.is_host)
        self.assertEqual(seen_room.code, room.code)

    def test_the_old_host_device_stops_being_the_host(self) -> None:
        room, first = self.hub.create_room(PASSWORD)
        opened_with = first.token
        self.hub.claim_host(room.code, PASSWORD)
        with self.assertRaises(GameError) as caught:
            self.hub.resolve_token(opened_with)
        self.assertEqual(caught.exception.code, "not_seated")

    def test_a_wrong_password_is_refused(self) -> None:
        room, _ = self.hub.create_room(PASSWORD)
        with self.assertRaises(GameError) as caught:
            self.hub.claim_host(room.code, "not the password")
        self.assertEqual(caught.exception.code, "bad_password")
        self.assertEqual(caught.exception.status_code, 403)

    def test_guessing_at_the_password_stops_being_answered(self) -> None:
        room, _ = self.hub.create_room(PASSWORD)
        for _ in range(buzzer.CLAIM_ATTEMPT_ALLOWANCE):
            with self.assertRaises(GameError):
                self.hub.claim_host(room.code, "wrong", now=100.0)

        with self.assertRaises(GameError) as caught:
            self.hub.claim_host(room.code, PASSWORD, now=101.0)
        self.assertEqual(caught.exception.code, "claims_blocked")
        self.assertEqual(caught.exception.status_code, 429)

        later = 101.0 + buzzer.CLAIM_BLOCK_SECONDS
        _, host = self.hub.claim_host(room.code, PASSWORD, now=later)
        self.assertTrue(host.is_host)

    def test_a_player_joins_with_a_code_and_a_name(self) -> None:
        room, _, players = room_with(self.hub, "Ava", "Ben")
        self.assertEqual(sorted(p.name for p in players), ["Ava", "Ben"])
        self.assertEqual(len(self.hub.players(room)), 2)

    def test_a_reloaded_ipad_sits_back_down_with_its_score(self) -> None:
        room, host, (ava,) = room_with(self.hub, "Ava")
        self.hub.adjust_score(room, host, ava.id, 400)

        _, back = self.hub.join_room(room.code, "ava")
        self.assertEqual(back.id, ava.id)
        self.assertEqual(back.score, 400)
        self.assertEqual(len(self.hub.players(room)), 1)

    def test_nobody_can_sit_in_the_host_chair_by_naming_themselves_host(self) -> None:
        room, _ = self.hub.create_room(PASSWORD)
        with self.assertRaises(GameError):
            self.hub.join_room(room.code, "host")

    def test_a_full_room_says_so(self) -> None:
        room, _ = self.hub.create_room(PASSWORD)
        for seat in range(MAX_PLAYERS):
            self.hub.join_room(room.code, f"Player {seat}")
        with self.assertRaises(GameError) as caught:
            self.hub.join_room(room.code, "One too many")
        self.assertIn("full", caught.exception.message)

    def test_an_unknown_code_is_not_a_room(self) -> None:
        with self.assertRaises(GameError) as caught:
            self.hub.join_room("ZZZZ", "Ava")
        self.assertEqual(caught.exception.code, "room_closed")


class TheButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))
        self.room, self.host, (self.ava, self.ben) = room_with(self.hub, "Ava", "Ben")

    def test_nothing_counts_until_the_host_opens_the_buzzers(self) -> None:
        with self.assertRaises(GameError) as caught:
            self.hub.buzz(self.room, self.ava)
        self.assertEqual(caught.exception.code, "too_early")
        self.assertEqual(self.room.phase, "idle")
        self.assertIsNone(self.room.winner)

    def test_the_first_tap_to_land_takes_it(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.hub.buzz(self.room, self.ben, now=10.3)

        with self.assertRaises(GameError) as caught:
            self.hub.buzz(self.room, self.ava, now=10.31)
        self.assertEqual(caught.exception.code, "too_late")
        self.assertIn("Ben", caught.exception.message)

        self.assertEqual(self.room.phase, "answering")
        self.assertEqual(self.room.winner.seat_id, self.ben.id)

    def test_the_taps_that_lost_do_not_write_to_the_room(self) -> None:
        """A class buzzing at once must not become a queue of writers."""

        class CountingStore(buzzer.MemoryStore):
            saves = 0

            def save(self, room: BuzzRoom) -> None:
                type(self).saves += 1
                super().save(room)

        hub = BuzzHub(rng=random.Random(0), store=CountingStore())
        room, host, players = room_with(hub, *[f"Player {n}" for n in range(8)])
        hub.open_buzzers(room, host, now=10.0)

        writes = CountingStore.saves
        hub.buzz(room, players[3], now=10.2)
        self.assertEqual(CountingStore.saves, writes + 1)

        for loser in players[:3] + players[4:]:
            with self.assertRaises(GameError):
                hub.buzz(room, loser, now=10.21)
        self.assertEqual(CountingStore.saves, writes + 1)

    def test_the_host_sees_how_long_the_winner_took(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        buzz = self.hub.buzz(self.room, self.ava, now=10.42)
        self.assertEqual(buzz.reaction_ms, 420)
        self.assertEqual(buzz.seat_id, self.ava.id)

        view = self.hub.view_for(self.room, self.host, now=10.5)
        self.assertEqual(view["winner"]["name"], "Ava")
        self.assertEqual(view["winner"]["reactionMs"], 420)

    def test_a_double_tap_is_the_same_buzz(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        first = self.hub.buzz(self.room, self.ava, now=10.2)
        again = self.hub.buzz(self.room, self.ava, now=10.25)
        self.assertEqual(again.seat_id, first.seat_id)
        self.assertEqual(again.at, first.at)
        self.assertEqual(self.room.winner, first)

    def test_the_host_does_not_buzz_in(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        with self.assertRaises(GameError) as caught:
            self.hub.buzz(self.room, self.host, now=10.1)
        self.assertEqual(caught.exception.code, "host_cannot_buzz")

    def test_opening_the_buzzers_again_mid_answer_is_refused(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.hub.buzz(self.room, self.ava, now=10.2)
        with self.assertRaises(GameError):
            self.hub.open_buzzers(self.room, self.host, now=10.3)

    def test_only_the_host_opens_the_buzzers(self) -> None:
        for action in (
            lambda: self.hub.open_buzzers(self.room, self.ava),
            lambda: self.hub.next_clue(self.room, self.ava),
            lambda: self.hub.reset_scores(self.room, self.ava),
            lambda: self.hub.judge(self.room, self.ava, True),
            lambda: self.hub.adjust_score(self.room, self.ava, self.ben.id, 100),
            lambda: self.hub.set_settings(self.room, self.ava, clue_value=400),
            lambda: self.hub.kick(self.room, self.ava, self.ben.id),
        ):
            with self.assertRaises(GameError) as caught:
                action()
            self.assertEqual(caught.exception.code, "host_only")


class FalseStartTests(unittest.TestCase):
    """Tapping through the open should not beat reacting to it."""

    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))
        self.room, self.host, (self.ava, self.ben) = room_with(self.hub, "Ava", "Ben")

    def test_an_early_tap_costs_the_start_of_the_window(self) -> None:
        with self.assertRaises(GameError):
            self.hub.buzz(self.room, self.ava, now=9.0)
        self.assertTrue(self.room.seats[self.ava.id].jumped)

        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.assertFalse(self.room.seats[self.ava.id].jumped)

        with self.assertRaises(GameError) as caught:
            self.hub.buzz(self.room, self.ava, now=10.0)
        self.assertEqual(caught.exception.code, "locked_out")

        # Somebody who waited for the light gets in while she is locked out.
        self.hub.buzz(self.room, self.ben, now=10.05)
        self.assertEqual(self.room.winner.seat_id, self.ben.id)

    def test_the_lockout_runs_out(self) -> None:
        with self.assertRaises(GameError):
            self.hub.buzz(self.room, self.ava, now=9.0)
        self.hub.open_buzzers(self.room, self.host, now=10.0)

        past = 10.0 + DEFAULT_FALSE_START_MS / 1000.0
        buzz = self.hub.buzz(self.room, self.ava, now=past)
        self.assertEqual(buzz.seat_id, self.ava.id)

    def test_a_clean_player_is_never_locked_out(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.assertEqual(self.hub.buzz(self.room, self.ben, now=10.0).seat_id, self.ben.id)

    def test_the_penalty_can_be_turned_off(self) -> None:
        self.hub.set_settings(self.room, self.host, false_start_ms=0)
        with self.assertRaises(GameError) as caught:
            self.hub.buzz(self.room, self.ava, now=9.0)
        self.assertEqual(caught.exception.code, "too_early")
        self.assertFalse(self.room.seats[self.ava.id].jumped)

        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.assertEqual(self.hub.buzz(self.room, self.ava, now=10.0).seat_id, self.ava.id)

    def test_an_earlier_lockout_does_not_follow_you_to_the_next_clue(self) -> None:
        with self.assertRaises(GameError):
            self.hub.buzz(self.room, self.ava, now=9.0)
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.hub.next_clue(self.room, self.host)

        self.hub.open_buzzers(self.room, self.host, now=20.0)
        self.assertEqual(self.hub.buzz(self.room, self.ava, now=20.0).seat_id, self.ava.id)


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))
        self.room, self.host, (self.ava, self.ben) = room_with(self.hub, "Ava", "Ben")

    def buzz_in(self, seat: Seat, at: float = 10.2) -> None:
        self.hub.open_buzzers(self.room, self.host, now=at - 0.2)
        self.hub.buzz(self.room, seat, now=at)

    def test_a_right_answer_scores_the_clue(self) -> None:
        self.hub.set_settings(self.room, self.host, clue_value=400)
        self.buzz_in(self.ava)
        self.hub.judge(self.room, self.host, True, now=11.0)

        self.assertEqual(self.room.seats[self.ava.id].score, 400)
        self.assertEqual(self.room.phase, "idle")
        self.assertEqual(self.room.clue_number, 2)
        self.assertEqual(self.room.last_result["name"], "Ava")

    def test_a_wrong_answer_costs_the_clue_and_leaves_the_rest_in(self) -> None:
        self.hub.set_settings(self.room, self.host, clue_value=400)
        self.buzz_in(self.ava)
        self.hub.judge(self.room, self.host, False, now=11.0)

        self.assertEqual(self.room.seats[self.ava.id].score, -400)
        self.assertEqual(self.room.phase, "idle")
        self.assertEqual(self.room.spent_ids, [self.ava.id])
        self.assertEqual(self.room.clue_number, 1)

        self.hub.open_buzzers(self.room, self.host, now=12.0)
        with self.assertRaises(GameError) as caught:
            self.hub.buzz(self.room, self.ava, now=12.1)
        self.assertEqual(caught.exception.code, "spent")
        self.assertEqual(self.hub.buzz(self.room, self.ben, now=12.2).seat_id, self.ben.id)

    def test_judging_with_nobody_in_is_refused(self) -> None:
        with self.assertRaises(GameError):
            self.hub.judge(self.room, self.host, True)

    def test_the_host_can_correct_a_score_by_hand(self) -> None:
        self.hub.adjust_score(self.room, self.host, self.ben.id, -200)
        self.assertEqual(self.room.seats[self.ben.id].score, -200)
        self.hub.reset_scores(self.room, self.host)
        self.assertEqual(self.room.seats[self.ben.id].score, 0)
        self.assertEqual(self.room.clue_number, 1)

    def test_the_scoreboard_leads_with_the_highest_score(self) -> None:
        self.hub.adjust_score(self.room, self.host, self.ben.id, 600)
        view = self.hub.view_for(self.room, self.host)
        self.assertEqual([row["name"] for row in view["players"]], ["Ben", "Ava"])

    def test_a_clue_cannot_be_worth_a_silly_amount(self) -> None:
        with self.assertRaises(GameError):
            self.hub.set_settings(self.room, self.host, clue_value=-1)
        with self.assertRaises(GameError):
            self.hub.set_settings(self.room, self.host, false_start_ms=99_999)


class LeavingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))
        self.room, self.host, (self.ava, self.ben) = room_with(self.hub, "Ava", "Ben")

    def test_removing_the_player_who_is_in_reopens_the_question(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.hub.buzz(self.room, self.ava, now=10.2)
        self.hub.kick(self.room, self.host, self.ava.id)

        self.assertEqual(self.room.phase, "idle")
        self.assertIsNone(self.room.winner)
        self.assertNotIn(self.ava.id, self.room.seats)

    def test_the_host_can_step_away_and_come_back(self) -> None:
        self.hub.leave(self.room, self.host)
        with self.assertRaises(GameError):
            self.hub.resolve_token(self.host.token)

        _, back = self.hub.claim_host(self.room.code, PASSWORD)
        self.assertTrue(back.is_host)

    def test_the_last_person_out_closes_the_room(self) -> None:
        self.hub.leave(self.room, self.ava)
        self.hub.leave(self.room, self.ben)
        self.hub.leave(self.room, self.host)
        self.assertIsNone(self.hub.store.load(self.room.code))

    def test_an_ipad_nobody_has_heard_from_shows_as_away(self) -> None:
        view = self.hub.view_for(self.room, self.host, now=time.time() + buzzer.AWAY_SECONDS + 1)
        self.assertTrue(all(row["away"] for row in view["players"]))


class WhatADeviceSeesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))
        self.room, self.host, (self.ava, self.ben) = room_with(self.hub, "Ava", "Ben")

    def test_a_view_never_carries_the_password_or_a_token(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.hub.buzz(self.room, self.ava, now=10.2)
        for seat in (self.host, self.ava):
            blob = json.dumps(self.hub.view_for(self.room, seat, now=10.5))
            self.assertNotIn(PASSWORD, blob)
            self.assertNotIn(self.room.password_hash, blob)
            self.assertNotIn(self.room.password_salt, blob)
            self.assertNotIn(seat.token, blob)
            self.assertNotIn(seat.token_hash, blob)

    def test_a_player_is_told_where_they_came_in(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.hub.buzz(self.room, self.ava, now=10.2)
        mine = self.hub.view_for(self.room, self.ava, now=10.5)["you"]
        self.assertTrue(mine["gotIn"])
        self.assertEqual(mine["reactionMs"], 200)

        theirs = self.hub.view_for(self.room, self.ben, now=10.5)["you"]
        self.assertFalse(theirs["gotIn"])

    def test_a_locked_out_player_is_told_for_how_long(self) -> None:
        with self.assertRaises(GameError):
            self.hub.buzz(self.room, self.ava, now=9.0)
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        mine = self.hub.view_for(self.room, self.ava, now=10.1)["you"]
        self.assertEqual(mine["lockedForMs"], DEFAULT_FALSE_START_MS - 100)

    def test_the_view_carries_the_server_clock(self) -> None:
        view = self.hub.view_for(self.room, self.host, now=1234.0)
        self.assertEqual(view["serverNow"], 1234.0)


class RevisionTests(unittest.TestCase):
    """A device watches this, so it has to move when the game moves."""

    def setUp(self) -> None:
        self.hub = BuzzHub(rng=random.Random(0))
        self.room, self.host, (self.ava, self.ben) = room_with(self.hub, "Ava", "Ben")

    def test_opening_the_buzzers_moves_it(self) -> None:
        before = state_revision(self.room)
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        self.assertNotEqual(state_revision(self.room), before)

    def test_a_buzz_moves_it(self) -> None:
        self.hub.open_buzzers(self.room, self.host, now=10.0)
        before = state_revision(self.room)
        self.hub.buzz(self.room, self.ava, now=10.2)
        self.assertNotEqual(state_revision(self.room), before)

    def test_a_score_moves_it(self) -> None:
        before = state_revision(self.room)
        self.hub.adjust_score(self.room, self.host, self.ava.id, 100)
        self.assertNotEqual(state_revision(self.room), before)

    def test_somebody_arriving_or_leaving_moves_it(self) -> None:
        before = state_revision(self.room)
        _, cara = self.hub.join_room(self.room.code, "Cara")
        after_join = state_revision(self.room)
        self.assertNotEqual(after_join, before)
        self.hub.kick(self.room, self.host, cara.id)
        self.assertEqual(state_revision(self.room), before)

    def test_an_ipad_only_saying_it_is_still_there_does_not_move_it(self) -> None:
        """Otherwise every quiet minute would wake the whole room."""
        before = state_revision(self.room)
        self.hub.resolve_token(self.ava.token, now=time.time() + 600)
        self.assertEqual(state_revision(self.room), before)


class SerializationTests(unittest.TestCase):
    def test_every_room_field_is_written(self) -> None:
        self.assertEqual({f.name for f in fields(BuzzRoom)}, serialized_room_fields())

    def test_every_seat_field_but_the_token_is_written(self) -> None:
        declared = {f.name for f in fields(Seat)} - {"token"}
        self.assertEqual(declared, serialized_seat_fields())

    def test_a_room_mid_answer_round_trips(self) -> None:
        hub = BuzzHub(rng=random.Random(0))
        room, host, (ava, ben) = room_with(hub, "Ava", "Ben")
        hub.set_settings(room, host, clue_value=800, false_start_ms=500)
        hub.open_buzzers(room, host, now=10.0)
        hub.buzz(room, ava, now=10.2)
        hub.judge(room, host, False, now=11.0)

        judged = room_from_dict(json.loads(json.dumps(room_to_dict(room))))
        self.assertEqual(judged.phase, "idle")
        self.assertEqual(judged.last_result["correct"], False)
        self.assertEqual(judged.last_result["name"], "Ava")

        hub.open_buzzers(room, host, now=12.0)
        hub.buzz(room, ben, now=12.4)

        restored = room_from_dict(json.loads(json.dumps(room_to_dict(room))))
        self.assertEqual(restored.phase, "answering")
        self.assertEqual(restored.clue_value, 800)
        self.assertEqual(restored.false_start_ms, 500)
        self.assertEqual(restored.spent_ids, [ava.id])
        self.assertEqual(restored.winner.seat_id, ben.id)
        self.assertEqual(restored.winner.reaction_ms, 400)
        self.assertEqual(restored.seats[ava.id].score, -800)
        # Opening the buzzers again clears the previous verdict off the wall.
        self.assertIsNone(restored.last_result)
        self.assertEqual(state_revision(restored), state_revision(room))

    def test_bearer_tokens_are_never_written_down(self) -> None:
        hub = BuzzHub(rng=random.Random(0))
        room, host, players = room_with(hub, "Ava", "Ben")
        blob = json.dumps(room_to_dict(room))
        for seat in [host, *players]:
            self.assertNotIn(seat.token, blob)
            self.assertIn(seat.token_hash, blob)
        self.assertEqual(room_from_dict(json.loads(blob)).seats[host.id].token, "")


class SharedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "rooms.db"

    def store(self):
        from store import SqliteStore

        store = SqliteStore(self.path, rooms=buzzer.BUZZER_ROOMS)
        self.addCleanup(store.close)
        return store

    def test_a_room_survives_a_restart_of_the_server(self) -> None:
        hub = BuzzHub(store=self.store())
        room, host, (ava,) = room_with(hub, "Ava")
        hub.open_buzzers(room, host, now=10.0)
        hub.buzz(room, ava, now=10.2)

        restarted = BuzzHub(store=self.store())
        seen_room, seen = restarted.resolve_token(host.token)
        self.assertEqual(seen_room.code, room.code)
        self.assertTrue(seen.is_host)
        self.assertEqual(seen_room.phase, "answering")
        self.assertEqual(seen_room.winner.seat_id, ava.id)

    def test_a_second_instance_sees_the_same_room(self) -> None:
        hub = BuzzHub(store=self.store())
        room, host, (ava, ben) = room_with(hub, "Ava", "Ben")
        hub.open_buzzers(room, host, now=10.0)

        other = BuzzHub(store=self.store())
        their_room, their_seat = other.resolve_token(ben.token)
        other.buzz(their_room, their_seat, now=10.1)

        back, _ = hub.resolve_token(ava.token)
        self.assertEqual(back.phase, "answering")
        self.assertEqual(back.winner.seat_id, ben.id)

    def test_a_buzzer_room_does_not_collide_with_an_imposter_room(self) -> None:
        from engine import GameHub
        from store import SqliteStore

        imposter_store = SqliteStore(self.path)
        self.addCleanup(imposter_store.close)
        buzz_hub = BuzzHub(store=self.store())
        room, _ = buzz_hub.create_room(PASSWORD)

        # The same four letters, dealt by the other game on the same evening.
        forced = GameHub(store=imposter_store)
        forced.store.save(_imposter_room(room.code))

        self.assertIsNotNone(buzz_hub.store.load(room.code))
        self.assertIsNotNone(imposter_store.load(room.code))
        self.assertTrue(buzz_hub.store.load(room.code).password_hash)


def _imposter_room(code: str):
    from engine import Room

    return Room(code=code, players={})


if __name__ == "__main__":
    unittest.main()

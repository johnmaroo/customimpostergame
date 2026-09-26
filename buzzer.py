"""A Jeopardy-style buzzer: one host console, a room full of iPads, one winner.

The host reads a clue out loud and opens the buzzers. Every iPad's button
lights up at the same moment and the first tap to reach the server takes the
buzz; everyone else is told who got in. The host marks the answer right or
wrong, the score moves, and the next clue starts.

Two things decide whether this feels fair, and both are handled here rather
than left to the network:

*Who was first* is the order the buzzes were **committed to the store**, not a
timestamp. Two instances of a serverless deployment do not share a clock, and
a few milliseconds of drift is an eternity next to two people reacting to the
same light. A compare-and-set write has exactly one winner, whoever's request
got there first, and a loser simply replays and lands second. Reaction times
are recorded alongside, but only so the room can see them.

*Tapping before the light* is a false start, and by default it costs the
tapper the first quarter second after the buzzers open, which is the rule a
real Jeopardy signalling device uses. That is why the button on a player's
iPad stays tappable when the buzzers are shut: the server, not the phone,
decides what a tap means, so holding a finger down through the open cannot
win anything.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import time
from dataclasses import dataclass, field, fields
from typing import Any, Literal

from engine import MemoryStore
from errors import GameError, StoreConflict
from sessions import (
    clean_name,
    hash_token,
    new_id,
    random_code,
    read_session_token,
    session_token,
)
from store import RoomKind

BUZZER_KEY_PREFIX = "buzzer:room:"
BUZZER_SCHEMA_VERSION = 1
MAX_PLAYERS = 40
HOST_NAME = "Host"

# Short enough to type on an iPad in front of a class, long enough that the
# throttle below makes guessing it not worth anybody's evening.
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128
PBKDF2_ROUNDS = 120_000
# Wrong passwords in a row before the room stops answering, and for how long.
CLAIM_ATTEMPT_ALLOWANCE = 5
CLAIM_BLOCK_SECONDS = 30.0

DEFAULT_CLUE_VALUE = 200
MAX_CLUE_VALUE = 100_000
DEFAULT_FALSE_START_MS = 250
MAX_FALSE_START_MS = 2_000

# A seat that has not been heard from in this long is shown as away. It has to
# sit well above the write interval below, or an iPad that is simply quiet
# between writes would be reported as gone.
AWAY_SECONDS = 120.0
SEAT_WRITE_SECONDS = 45.0

Phase = Literal["idle", "open", "answering"]


class BuzzError(GameError):
    """A buzz that did not count, with a code the iPad can turn into feedback."""


@dataclass
class Seat:
    id: str
    name: str
    token: str = ""
    token_hash: str = ""
    is_host: bool = False
    score: int = 0
    last_seen: float = field(default_factory=time.time)
    # Tapped while the buzzers were shut, and owes a lockout on the next open.
    jumped: bool = False
    # Server clock until which this seat's taps do not count.
    locked_until: float = 0.0

    def __post_init__(self) -> None:
        if not self.token_hash and self.token:
            self.token_hash = hash_token(self.token)


@dataclass
class Buzz:
    """The tap that took the clue: who, when, and how long after the light."""

    seat_id: str
    at: float
    reaction_ms: int | None


@dataclass
class BuzzRoom:
    code: str
    seats: dict[str, Seat]
    password_salt: str = ""
    password_hash: str = ""
    password_rounds: int = PBKDF2_ROUNDS
    failed_claims: int = 0
    claims_blocked_until: float = 0.0
    phase: Phase = "idle"
    opened_at: float | None = None
    # Whoever got in. Only the first tap is recorded: the taps behind it are
    # turned away without a write, so a room of iPads all going at once does
    # not become a room of iPads all queueing to write to the same room.
    winner: Buzz | None = None
    # Answered this clue already and got it wrong, so they sit the rest out.
    spent_ids: list[str] = field(default_factory=list)
    clue_value: int = DEFAULT_CLUE_VALUE
    false_start_ms: int = DEFAULT_FALSE_START_MS
    clue_number: int = 1
    last_result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    version: int = 0


def hash_password(password: str, salt: str, rounds: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds
    ).hex()


def clean_password(password: str) -> str:
    """Take the password as typed, minus the spaces an iPad keyboard adds."""
    cleaned = (password or "").strip()
    if len(cleaned) < MIN_PASSWORD_LEN:
        raise GameError(f"Use a host password of at least {MIN_PASSWORD_LEN} characters.")
    if len(cleaned) > MAX_PASSWORD_LEN:
        raise GameError("That host password is too long.")
    return cleaned


def seat_to_dict(seat: Seat) -> dict[str, Any]:
    return {
        "id": seat.id,
        "name": seat.name,
        "tokenHash": seat.token_hash,
        "isHost": seat.is_host,
        "score": seat.score,
        "lastSeen": seat.last_seen,
        "jumped": seat.jumped,
        "lockedUntil": seat.locked_until,
    }


def seat_from_dict(data: dict[str, Any]) -> Seat:
    return Seat(
        id=data["id"],
        name=data["name"],
        token="",
        token_hash=data.get("tokenHash") or "",
        is_host=bool(data.get("isHost")),
        score=int(data.get("score") or 0),
        last_seen=float(data.get("lastSeen") or 0.0),
        jumped=bool(data.get("jumped")),
        locked_until=float(data.get("lockedUntil") or 0.0),
    )


def buzz_to_dict(buzz: Buzz) -> dict[str, Any]:
    return {"seatId": buzz.seat_id, "at": buzz.at, "reactionMs": buzz.reaction_ms}


def buzz_from_dict(data: dict[str, Any]) -> Buzz:
    reaction = data.get("reactionMs")
    return Buzz(
        seat_id=data["seatId"],
        at=float(data.get("at") or 0.0),
        reaction_ms=None if reaction is None else int(reaction),
    )


def room_to_dict(room: BuzzRoom) -> dict[str, Any]:
    """JSON-ready room. Bearer tokens and the password are only ever hashes."""
    return {
        "schema": BUZZER_SCHEMA_VERSION,
        "code": room.code,
        "seats": [seat_to_dict(seat) for seat in room.seats.values()],
        "passwordSalt": room.password_salt,
        "passwordHash": room.password_hash,
        "passwordRounds": room.password_rounds,
        "failedClaims": room.failed_claims,
        "claimsBlockedUntil": room.claims_blocked_until,
        "phase": room.phase,
        "openedAt": room.opened_at,
        "winner": buzz_to_dict(room.winner) if room.winner else None,
        "spentIds": list(room.spent_ids),
        "clueValue": room.clue_value,
        "falseStartMs": room.false_start_ms,
        "clueNumber": room.clue_number,
        "lastResult": dict(room.last_result) if room.last_result else None,
        "createdAt": room.created_at,
        "updatedAt": room.updated_at,
        "lastSeenAt": room.last_seen_at,
        "version": room.version,
    }


def room_from_dict(data: dict[str, Any]) -> BuzzRoom:
    seats = {row["id"]: seat_from_dict(row) for row in data.get("seats") or []}
    created = float(data.get("createdAt") or time.time())
    updated = float(data.get("updatedAt") or created)
    return BuzzRoom(
        code=data["code"],
        seats=seats,
        password_salt=data.get("passwordSalt") or "",
        password_hash=data.get("passwordHash") or "",
        password_rounds=int(data.get("passwordRounds") or PBKDF2_ROUNDS),
        failed_claims=int(data.get("failedClaims") or 0),
        claims_blocked_until=float(data.get("claimsBlockedUntil") or 0.0),
        phase=data.get("phase") or "idle",
        opened_at=data.get("openedAt"),
        winner=buzz_from_dict(data["winner"]) if data.get("winner") else None,
        spent_ids=list(data.get("spentIds") or []),
        clue_value=int(data.get("clueValue") or DEFAULT_CLUE_VALUE),
        false_start_ms=int(data.get("falseStartMs") or 0),
        clue_number=int(data.get("clueNumber") or 1),
        last_result=dict(data["lastResult"]) if data.get("lastResult") else None,
        created_at=created,
        updated_at=updated,
        last_seen_at=float(data.get("lastSeenAt") or updated),
        version=int(data.get("version") or 0),
    )


def serialized_room_fields() -> set[str]:
    """Field names covered by `room_to_dict`, so new state cannot slip through."""
    return {
        "code",
        "seats",
        "password_salt",
        "password_hash",
        "password_rounds",
        "failed_claims",
        "claims_blocked_until",
        "phase",
        "opened_at",
        "winner",
        "spent_ids",
        "clue_value",
        "false_start_ms",
        "clue_number",
        "last_result",
        "created_at",
        "updated_at",
        "last_seen_at",
        "version",
    }


def serialized_seat_fields() -> set[str]:
    """Field names covered by `seat_to_dict`.

    Everything a seat holds except `token`: the bearer token itself is only
    ever written down as the hash beside it.
    """
    return {
        "id",
        "name",
        "token_hash",
        "is_host",
        "score",
        "last_seen",
        "jumped",
        "locked_until",
    }


BUZZER_ROOMS = RoomKind(BUZZER_KEY_PREFIX, room_to_dict, room_from_dict)


def refresh_room(target: BuzzRoom, source: BuzzRoom) -> None:
    """Pull stored state into the copy this process already handed out.

    A caller holds on to `BuzzRoom` and `Seat` objects across calls, so a
    reload has to update those objects in place rather than swap in new ones
    the caller is not holding.
    """
    for spot in fields(BuzzRoom):
        if spot.name != "seats":
            setattr(target, spot.name, getattr(source, spot.name))
    for seat_id, incoming in source.seats.items():
        seated = target.seats.get(seat_id)
        if seated is None:
            target.seats[seat_id] = incoming
            continue
        for spot in fields(Seat):
            if spot.name == "token" and not incoming.token:
                continue  # stored seats only keep the hash; keep a fresh token
            setattr(seated, spot.name, getattr(incoming, spot.name))
    for seat_id in [known for known in target.seats if known not in source.seats]:
        del target.seats[seat_id]


class BuzzHub:
    """Every room of iPads, and the rules the host's button runs on."""

    def __init__(
        self,
        rng: random.Random | None = None,
        store: Any = None,
        idle_seconds: float = 4 * 60 * 60,
    ) -> None:
        self.store = MemoryStore() if store is None else store
        self.idle_seconds = idle_seconds
        self.rng = rng if rng is not None else random.SystemRandom()
        self._live: dict[str, BuzzRoom] = {}

    # -- getting in ------------------------------------------------------

    def create_room(self, password: str) -> tuple[BuzzRoom, Seat]:
        cleaned = clean_password(password)
        code = self._unique_code()
        salt = secrets.token_bytes(16).hex()
        host = self._seat(code, HOST_NAME, is_host=True)
        room = BuzzRoom(
            code=code,
            seats={host.id: host},
            password_salt=salt,
            password_hash=hash_password(cleaned, salt, PBKDF2_ROUNDS),
            password_rounds=PBKDF2_ROUNDS,
        )
        self._live[code] = room
        self._touch(room)
        return room, host

    def claim_host(self, code: str, password: str, now: float | None = None) -> tuple[BuzzRoom, Seat]:
        """Hand the console to whichever device can prove it knows the password.

        There is one host seat, and claiming it rotates its token, so a host
        who moves to another iPad takes the console with them and the old
        device stops being the host.
        """
        clock = time.time() if now is None else now
        room = self._room(code)
        if clock < room.claims_blocked_until:
            wait = int(room.claims_blocked_until - clock) + 1
            raise GameError(
                f"Too many wrong passwords. Try again in {wait} seconds.",
                429,
                "claims_blocked",
            )
        host = self._host_seat(room)
        supplied = (password or "").strip()
        expected = room.password_hash
        offered = hash_password(supplied, room.password_salt, room.password_rounds)
        if not expected or not secrets.compare_digest(expected, offered):
            self._note_failed_claim(room, clock)
            raise GameError("That host password is not right.", 403, "bad_password")
        room.failed_claims = 0
        room.claims_blocked_until = 0.0
        host.token = session_token(room.code, host.id)
        host.token_hash = hash_token(host.token)
        host.last_seen = clock
        self._touch(room)
        return room, host

    def join_room(self, code: str, name: str, now: float | None = None) -> tuple[BuzzRoom, Seat]:
        clock = time.time() if now is None else now
        room = self._room(code)
        cleaned = clean_name(name)
        if cleaned.casefold() == HOST_NAME.casefold():
            raise GameError("Pick a different name — that one belongs to the host.")
        existing = next(
            (
                seat
                for seat in room.seats.values()
                if not seat.is_host and seat.name.casefold() == cleaned.casefold()
            ),
            None,
        )
        if existing is not None:
            # An iPad that reloaded or locked its screen gets its score back.
            existing.token = session_token(room.code, existing.id)
            existing.token_hash = hash_token(existing.token)
            existing.last_seen = clock
            self._touch(room)
            return room, existing
        if len(self.players(room)) >= MAX_PLAYERS:
            raise GameError(f"This room is full ({MAX_PLAYERS} players).")
        seat = self._seat(room.code, cleaned, is_host=False)
        seat.last_seen = clock
        room.seats[seat.id] = seat
        self._touch(room)
        return room, seat

    def resolve_token(self, token: str | None, now: float | None = None) -> tuple[BuzzRoom, Seat]:
        if not token:
            raise GameError("Join this room first.", 401, "no_session")
        parsed = read_session_token(token)
        if parsed is None:
            raise GameError("Session expired. Join again with the same name.", 401, "session_invalid")
        code, seat_id = parsed
        room = self._load(code)
        if room is None:
            raise GameError("That room has closed.", 404, "room_closed")
        seat = room.seats.get(seat_id)
        if seat is None or not secrets.compare_digest(seat.token_hash, hash_token(token)):
            raise GameError(
                "You are no longer in this room. Join again with the same name.",
                401,
                "not_seated",
            )
        self._mark_seen(room, seat, now=now)
        return room, seat

    def leave(self, room: BuzzRoom, seat: Seat) -> None:
        if seat.is_host:
            # The console can be picked up again with the password, so the
            # seat stays and only its token goes; dropping the seat would
            # leave a room full of iPads that nobody can open the buzzers on.
            seat.token = ""
            seat.token_hash = ""
            if self._closed(room):
                return
            self._touch(room)
            return
        self._drop_seat(room, seat.id)

    def kick(self, room: BuzzRoom, host: Seat, seat_id: str) -> None:
        self._require_host(host)
        target = room.seats.get(seat_id)
        if target is None or target.is_host:
            raise GameError("That player is not in this room.")
        self._drop_seat(room, seat_id)

    # -- the button ------------------------------------------------------

    def open_buzzers(self, room: BuzzRoom, host: Seat, now: float | None = None) -> None:
        self._require_host(host)
        if room.phase == "answering":
            raise GameError("Somebody is already in. Judge the answer first.")
        clock = time.time() if now is None else now
        penalty = room.false_start_ms / 1000.0
        for seat in room.seats.values():
            if seat.jumped:
                seat.locked_until = clock + penalty
                seat.jumped = False
            elif seat.locked_until:
                seat.locked_until = 0.0
        room.phase = "open"
        room.opened_at = clock
        room.winner = None
        room.last_result = None
        self._touch(room, at=clock)

    def buzz(self, room: BuzzRoom, seat: Seat, now: float | None = None) -> Buzz:
        """Take one tap. Whether it counts is decided here, never on the iPad.

        Everything but the winning tap is turned away without writing to the
        room, which is what lets a whole class buzz at once: only one request
        has to win a compare-and-set, and the rest are told who beat them.
        """
        clock = time.time() if now is None else now
        if seat.is_host:
            raise BuzzError("The host does not buzz in.", 403, "host_cannot_buzz")
        if room.winner is not None and room.winner.seat_id == seat.id:
            # A double tap, or a retry of a request that did land. Same answer.
            return room.winner
        if seat.id in room.spent_ids:
            raise BuzzError("You already had a go at this one.", 409, "spent")
        if room.phase == "answering":
            raise BuzzError(self._who_is_in(room), 409, "too_late")
        if room.phase != "open":
            if room.false_start_ms > 0 and not seat.jumped:
                seat.jumped = True
                self._touch(room, at=clock)
            raise BuzzError("Too early. Wait for the buzzers.", 409, "too_early")
        if clock < seat.locked_until:
            raise BuzzError(
                "Locked out for a moment — you buzzed before the clue.",
                409,
                "locked_out",
            )

        reaction = (
            None if room.opened_at is None else max(0, round((clock - room.opened_at) * 1000))
        )
        room.winner = Buzz(seat_id=seat.id, at=clock, reaction_ms=reaction)
        room.phase = "answering"
        self._touch(room, at=clock)
        return room.winner

    def judge(self, room: BuzzRoom, host: Seat, correct: bool, now: float | None = None) -> None:
        """Score whoever is in, then shut the buzzers until the host opens them."""
        self._require_host(host)
        if room.phase != "answering" or room.winner is None:
            raise GameError("Nobody has buzzed in yet.")
        clock = time.time() if now is None else now
        winner = room.winner
        seat = room.seats.get(winner.seat_id)
        value = room.clue_value
        if seat is not None:
            seat.score += value if correct else -value
        room.last_result = {
            "seatId": winner.seat_id,
            "name": seat.name if seat else "Someone",
            "correct": bool(correct),
            "value": value,
            "clueNumber": room.clue_number,
            "at": clock,
        }
        room.phase = "idle"
        room.opened_at = None
        room.winner = None
        if correct:
            room.spent_ids = []
            room.clue_number += 1
        elif winner.seat_id not in room.spent_ids:
            # Wrong answer: they are out of this clue, the rest are still in.
            room.spent_ids.append(winner.seat_id)
        self._touch(room, at=clock)

    def next_clue(self, room: BuzzRoom, host: Seat) -> None:
        """Shut the buzzers and start over: nobody in, nobody locked out."""
        self._require_host(host)
        moving_on = room.phase != "idle" or room.spent_ids or room.last_result
        room.phase = "idle"
        room.opened_at = None
        room.winner = None
        room.spent_ids = []
        room.last_result = None
        for seat in room.seats.values():
            seat.jumped = False
            seat.locked_until = 0.0
        if moving_on:
            room.clue_number += 1
        self._touch(room)

    # -- the scoreboard --------------------------------------------------

    def adjust_score(self, room: BuzzRoom, host: Seat, seat_id: str, delta: int) -> None:
        self._require_host(host)
        seat = room.seats.get(seat_id)
        if seat is None or seat.is_host:
            raise GameError("That player is not in this room.")
        if abs(int(delta)) > MAX_CLUE_VALUE:
            raise GameError("That is more than a clue can be worth.")
        seat.score += int(delta)
        self._touch(room)

    def reset_scores(self, room: BuzzRoom, host: Seat) -> None:
        self._require_host(host)
        for seat in room.seats.values():
            seat.score = 0
        room.clue_number = 1
        room.last_result = None
        self._touch(room)

    def set_settings(
        self,
        room: BuzzRoom,
        host: Seat,
        *,
        clue_value: int | None = None,
        false_start_ms: int | None = None,
    ) -> None:
        self._require_host(host)
        if clue_value is not None:
            value = int(clue_value)
            if value < 0 or value > MAX_CLUE_VALUE:
                raise GameError(f"Clues can be worth 0 to {MAX_CLUE_VALUE} points.")
            room.clue_value = value
        if false_start_ms is not None:
            penalty = int(false_start_ms)
            if penalty < 0 or penalty > MAX_FALSE_START_MS:
                raise GameError(
                    f"An early-buzz lockout can be 0 to {MAX_FALSE_START_MS} milliseconds."
                )
            room.false_start_ms = penalty
        self._touch(room)

    # -- what a device sees ----------------------------------------------

    def players(self, room: BuzzRoom) -> list[Seat]:
        return [seat for seat in room.seats.values() if not seat.is_host]

    def view_for(self, room: BuzzRoom, seat: Seat, *, now: float | None = None) -> dict[str, Any]:
        clock = time.time() if now is None else now
        names = {member.id: member.name for member in room.seats.values()}
        winner = room.winner
        players = [
            {
                "id": member.id,
                "name": member.name,
                "score": member.score,
                "away": clock - member.last_seen > AWAY_SECONDS,
                "spent": member.id in room.spent_ids,
            }
            for member in self.players(room)
        ]
        players.sort(key=lambda row: (-row["score"], row["name"].casefold()))
        i_got_in = winner is not None and winner.seat_id == seat.id
        return {
            "code": room.code,
            "phase": room.phase,
            "clueNumber": room.clue_number,
            "clueValue": room.clue_value,
            "falseStartMs": room.false_start_ms,
            "openedAt": room.opened_at,
            # A device with a wrong clock can still show how long the buzzers
            # have been open, by measuring against this instead of its own.
            "serverNow": clock,
            "winner": (
                None
                if winner is None
                else {
                    "seatId": winner.seat_id,
                    "name": names.get(winner.seat_id, "Someone"),
                    "reactionMs": winner.reaction_ms,
                }
            ),
            "players": players,
            "lastResult": dict(room.last_result) if room.last_result else None,
            "you": {
                "id": seat.id,
                "name": seat.name,
                "isHost": seat.is_host,
                "score": seat.score,
                "spent": seat.id in room.spent_ids,
                "lockedForMs": max(0, round((seat.locked_until - clock) * 1000)),
                "gotIn": i_got_in,
                "reactionMs": winner.reaction_ms if i_got_in else None,
            },
            "rev": state_revision(room),
            "updatedAt": room.updated_at,
        }

    def sweep_idle(self, now: float | None = None) -> None:
        clock = time.time() if now is None else now
        cutoff = clock - self.idle_seconds
        self.store.sweep(cutoff)
        for code in [key for key, room in self._live.items() if room.last_seen_at < cutoff]:
            self._live.pop(code, None)

    # -- plumbing ---------------------------------------------------------

    def _seat(self, code: str, name: str, *, is_host: bool) -> Seat:
        seat_id = new_id()
        return Seat(
            id=seat_id,
            name=name,
            token=session_token(code, seat_id),
            is_host=is_host,
        )

    def _host_seat(self, room: BuzzRoom) -> Seat:
        host = next((seat for seat in room.seats.values() if seat.is_host), None)
        if host is None:
            # Only reachable if a room were written without one; rebuild it
            # rather than stranding a room nobody can host.
            host = self._seat(room.code, HOST_NAME, is_host=True)
            room.seats[host.id] = host
        return host

    def _note_failed_claim(self, room: BuzzRoom, clock: float) -> None:
        room.failed_claims += 1
        if room.failed_claims >= CLAIM_ATTEMPT_ALLOWANCE:
            over = room.failed_claims - CLAIM_ATTEMPT_ALLOWANCE
            room.claims_blocked_until = clock + CLAIM_BLOCK_SECONDS * (2**over)
        try:
            self._touch(room, at=clock)
        except StoreConflict:
            # Somebody else wrote the room between the read and now. Counting
            # this attempt matters less than answering the one in front of us.
            pass

    def _who_is_in(self, room: BuzzRoom) -> str:
        seat = room.seats.get(room.winner.seat_id) if room.winner else None
        return f"{seat.name} got in first." if seat else "Somebody got in first."

    def _require_host(self, seat: Seat) -> None:
        if not seat.is_host:
            raise GameError("Only the host can do that.", 403, "host_only")

    def _drop_seat(self, room: BuzzRoom, seat_id: str) -> None:
        room.seats.pop(seat_id, None)
        room.spent_ids = [spent for spent in room.spent_ids if spent != seat_id]
        if room.winner is not None and room.winner.seat_id == seat_id:
            # The person who was in has gone. Shut the buzzers rather than
            # leaving the host with an answer to judge and nobody to judge.
            room.winner = None
            room.phase = "idle"
            room.opened_at = None
        if self._closed(room):
            return
        self._touch(room)

    def _closed(self, room: BuzzRoom) -> bool:
        """Drop a room once there is nobody in it and nobody hosting it."""
        if self.players(room) or any(seat.token_hash for seat in room.seats.values()):
            return False
        self._live.pop(room.code, None)
        self.store.delete(room.code)
        return True

    def _room(self, code: str) -> BuzzRoom:
        room = self._load((code or "").strip().upper())
        if room is None:
            raise GameError("No buzzer room with that code.", 404, "room_closed")
        return room

    def _load(self, code: str) -> BuzzRoom | None:
        """Read a room, reusing the copy this process already has of it."""
        stored = self.store.load(code)
        if stored is None:
            self._live.pop(code, None)
            return None
        live = self._live.get(code)
        if live is None or live is stored:
            self._live[code] = stored
            return stored
        refresh_room(live, stored)
        return live

    def _unique_code(self) -> str:
        for _ in range(50):
            code = random_code(self.rng)
            if self._load(code) is None:
                return code
        raise GameError("Could not open a room. Try again.", 500)

    def _touch(self, room: BuzzRoom, at: float | None = None) -> None:
        room.updated_at = time.time() if at is None else at
        room.last_seen_at = max(room.last_seen_at, room.updated_at)
        self.store.save(room)

    def _mark_seen(self, room: BuzzRoom, seat: Seat, now: float | None = None) -> None:
        """Reading the room keeps it alive, without a store write per request."""
        clock = time.time() if now is None else now
        seat.last_seen = clock
        if clock - room.last_seen_at < SEAT_WRITE_SECONDS:
            return
        room.last_seen_at = clock
        try:
            self.store.save(room)
        except StoreConflict:
            # Whoever beat us to it refreshed the room anyway.
            pass


def state_revision(room: BuzzRoom) -> str:
    """A short stand-in for 'the part of the room a device is looking at'.

    A device watching for a change compares this rather than the store's
    version, because the version also moves when a seat is only saying it is
    still there. Waking every iPad in the room for that would undo the point
    of watching.
    """
    payload = json.dumps(
        [
            room.phase,
            room.opened_at,
            room.clue_number,
            room.clue_value,
            room.false_start_ms,
            None if room.winner is None else [room.winner.seat_id, room.winner.reaction_ms],
            sorted(room.spent_ids),
            sorted((seat.id, seat.name, seat.score, seat.is_host) for seat in room.seats.values()),
            room.last_result,
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

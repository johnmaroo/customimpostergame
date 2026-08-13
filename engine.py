"""In-memory party sessions for Imposter.

The web UI and tests both drive this module. Secrets (the round's word and
who the imposters are) stay on the server and are only placed in a player's
own view — never in the shared room snapshot.
"""

from __future__ import annotations

import random
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"
MIN_PLAYERS = 3
MAX_NAME_LEN = 24
MAX_WORD_LEN = 48
MAX_WORDS_PER_ROOM = 200
ROOM_IDLE_SECONDS = 4 * 60 * 60

Phase = Literal["lobby", "reveal", "discuss", "huddle", "guess", "vote", "results", "ended"]
Winner = Literal["faithfuls", "imposters"]
WinReason = Literal["guess", "vote"]


class GameError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise GameError("Enter a name to join.")
    if len(cleaned) > MAX_NAME_LEN:
        raise GameError(f"Names can be at most {MAX_NAME_LEN} characters.")
    return cleaned


def _clean_word(word: str) -> str:
    cleaned = " ".join((word or "").split())
    if not cleaned:
        raise GameError("Enter a word.")
    if len(cleaned) > MAX_WORD_LEN:
        raise GameError(f"Words can be at most {MAX_WORD_LEN} characters.")
    return cleaned


def _normalize_guess(text: str) -> str:
    pieces: list[str] = []
    for ch in text or "":
        if ch.isalnum() or ch.isspace():
            pieces.append(ch.lower())
        else:
            pieces.append(" ")
    return " ".join("".join(pieces).split())


def _new_id() -> str:
    return secrets.token_urlsafe(9)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class Player:
    id: str
    name: str
    token: str
    is_host: bool
    score: int = 0
    connected: bool = True
    phone: str | None = None


@dataclass
class Invite:
    token: str
    name: str
    phone: str
    claimed_by: str | None = None


@dataclass
class RoundState:
    word: str
    clue: str | None
    imposter_ids: list[str]
    participant_ids: list[str]
    speaking_order: list[str]
    speaker_index: int = 0
    lap: int = 1
    ready_ids: set[str] = field(default_factory=set)
    votes: dict[str, str | None] = field(default_factory=dict)
    guesses: dict[str, str] = field(default_factory=dict)
    discuss_ends_at: float | None = None
    winner: Winner | None = None
    win_reason: WinReason | None = None
    guessed_by: str | None = None
    clue_unlocked: bool = False
    eliminated_ids: list[str] = field(default_factory=list)
    score_delta: dict[str, int] = field(default_factory=dict)
    vote_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Room:
    code: str
    players: dict[str, Player]
    remaining_words: list[str] = field(default_factory=list)
    used_words: list[str] = field(default_factory=list)
    num_imposters: int = 1
    discuss_seconds: int = 90
    pass_and_play: bool = False
    words_visible: bool = False
    phase: Phase = "lobby"
    round: RoundState | None = None
    round_number: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    invites: dict[str, Invite] = field(default_factory=dict)


class GameHub:
    def __init__(self, rng: random.Random | None = None) -> None:
        self.rooms: dict[str, Room] = {}
        self.token_index: dict[str, tuple[str, str]] = {}
        self.invite_index: dict[str, str] = {}
        self.rng = rng or random.Random()

    def create_room(self, host_name: str) -> tuple[Room, Player]:
        name = _clean_name(host_name)
        code = self._unique_code()
        host = Player(id=_new_id(), name=name, token=_new_token(), is_host=True)
        room = Room(code=code, players={host.id: host})
        self.rooms[code] = room
        self.token_index[host.token] = (code, host.id)
        return room, host

    def join_room(
        self,
        code: str,
        name: str,
        invite_token: str | None = None,
    ) -> tuple[Room, Player]:
        room = self._room(code)
        invite = None
        if invite_token:
            invite = room.invites.get(invite_token.strip())
            if invite is None:
                raise GameError("That invite is no longer valid.")
            if invite.claimed_by and invite.claimed_by in room.players:
                raise GameError("That invite was already used.")
        cleaned = _clean_name(name or (invite.name if invite else ""))
        taken = {p.name.casefold() for p in room.players.values()}
        if cleaned.casefold() in taken:
            raise GameError("That name is already in this room.")
        if len(room.players) >= 16:
            raise GameError("This room is full (16 players).")
        player = Player(
            id=_new_id(),
            name=cleaned,
            token=_new_token(),
            is_host=False,
            phone=invite.phone if invite else None,
        )
        room.players[player.id] = player
        self.token_index[player.token] = (room.code, player.id)
        if invite:
            invite.claimed_by = player.id
        self._touch(room)
        return room, player

    def add_invite(self, room: Room, host: Player, name: str, phone: str) -> Invite:
        self._require_host(host)
        if room.phase not in ("lobby", "results"):
            raise GameError("Invite people between rounds.")
        cleaned_name = _clean_name(name)
        cleaned_phone = (phone or "").strip()
        if not cleaned_phone:
            raise GameError("Enter a phone number.")
        for existing in room.invites.values():
            claimed_here = existing.claimed_by and existing.claimed_by in room.players
            if existing.phone == cleaned_phone and (not existing.claimed_by or claimed_here):
                raise GameError("That number already has an invite.")
        invite = Invite(token=_new_token(), name=cleaned_name, phone=cleaned_phone)
        room.invites[invite.token] = invite
        self.invite_index[invite.token] = room.code
        self._touch(room)
        return invite

    def lookup_invite(self, token: str) -> tuple[Room, Invite]:
        code = self.invite_index.get((token or "").strip())
        if not code:
            raise GameError("That invite is no longer valid.", 404)
        room = self.rooms.get(code)
        if room is None:
            raise GameError("That room has closed.", 404)
        invite = room.invites.get(token.strip())
        if invite is None:
            raise GameError("That invite is no longer valid.", 404)
        if invite.claimed_by and invite.claimed_by in room.players:
            raise GameError("That invite was already used.")
        return room, invite

    def resolve_token(self, token: str | None) -> tuple[Room, Player]:
        if not token:
            raise GameError("Sign in to this room first.", 401)
        found = self.token_index.get(token)
        if not found:
            raise GameError("Session expired. Join the room again.", 401)
        code, player_id = found
        room = self.rooms.get(code)
        if room is None:
            raise GameError("That room has closed.", 404)
        player = room.players.get(player_id)
        if player is None:
            raise GameError("You are no longer in this room.", 401)
        player.connected = True
        return room, player

    def leave(self, room: Room, player: Player) -> None:
        if room.phase not in ("lobby", "results", "ended") and player.id in self._participants(room):
            raise GameError("You can leave between rounds.")
        self._remove_player(room, player)

    def kick(self, room: Room, host: Player, target_id: str) -> None:
        self._require_host(host)
        if room.phase not in ("lobby", "results"):
            raise GameError("Wait until the round is over to remove someone.")
        target = room.players.get(target_id)
        if target is None:
            raise GameError("That player is not in the room.")
        if target.is_host:
            raise GameError("The host cannot be removed.")
        self._remove_player(room, target)

    def end_game(self, room: Room, host: Player) -> None:
        self._require_host(host)
        room.phase = "ended"
        self._touch(room)

    def reopen_lobby(self, room: Room, host: Player) -> None:
        self._require_host(host)
        if room.phase != "ended":
            raise GameError("The game is still going.")
        room.phase = "lobby"
        room.round = None
        self._touch(room)

    def set_settings(
        self,
        room: Room,
        host: Player,
        *,
        num_imposters: int | None = None,
        discuss_seconds: int | None = None,
        pass_and_play: bool | None = None,
        words_visible: bool | None = None,
    ) -> None:
        self._require_host(host)
        if room.phase not in ("lobby", "results"):
            raise GameError("Settings can change between rounds.")
        if num_imposters is not None:
            if num_imposters < 1:
                raise GameError("You need at least one imposter.")
            room.num_imposters = num_imposters
        if discuss_seconds is not None:
            if discuss_seconds not in (0, 45, 60, 90, 120, 180, 300):
                raise GameError("Pick a discussion length from the list.")
            room.discuss_seconds = discuss_seconds
        if pass_and_play is not None:
            room.pass_and_play = bool(pass_and_play)
        if words_visible is not None:
            room.words_visible = bool(words_visible)
        self._touch(room)

    def add_word(self, room: Room, host: Player, word: str) -> str:
        self._require_host(host)
        cleaned = _clean_word(word)
        if len(room.remaining_words) + len(room.used_words) >= MAX_WORDS_PER_ROOM:
            raise GameError("Word bank is full.")
        already = {item.casefold() for item in room.remaining_words + room.used_words}
        if cleaned.casefold() in already:
            if cleaned.casefold() in {item.casefold() for item in room.remaining_words}:
                return cleaned
            room.used_words = [w for w in room.used_words if w.casefold() != cleaned.casefold()]
            room.remaining_words.append(cleaned)
            self._touch(room)
            return cleaned
        room.remaining_words.append(cleaned)
        self._touch(room)
        return cleaned

    def add_words(self, room: Room, host: Player, words: list[str]) -> list[str]:
        added: list[str] = []
        for word in words:
            added.append(self.add_word(room, host, word))
        return added

    def remove_word(self, room: Room, host: Player, word: str) -> None:
        self._require_host(host)
        if room.phase not in ("lobby", "results"):
            raise GameError("Wait until the round is over to edit the word bank.")
        target = _clean_word(word)
        before = len(room.remaining_words) + len(room.used_words)
        room.remaining_words = [w for w in room.remaining_words if w.casefold() != target.casefold()]
        room.used_words = [w for w in room.used_words if w.casefold() != target.casefold()]
        if len(room.remaining_words) + len(room.used_words) == before:
            raise GameError("That word is not in this game.")
        self._touch(room)

    def recycle_words(self, room: Room, host: Player) -> int:
        self._require_host(host)
        if room.phase not in ("lobby", "results"):
            raise GameError("Wait until the round is over.")
        n = len(room.used_words)
        room.remaining_words.extend(room.used_words)
        room.used_words = []
        self._touch(room)
        return n

    def start_round(self, room: Room, host: Player, *, clue: str | None = None, now: float | None = None) -> RoundState:
        self._require_host(host)
        self.tick(room, now=now)
        if room.phase == "ended":
            raise GameError("This game has ended. Play again to start a new round.")
        if room.phase not in ("lobby", "results"):
            raise GameError("A round is already in progress.")
        seated = list(room.players.values())
        if len(seated) < MIN_PLAYERS:
            raise GameError(f"Need at least {MIN_PLAYERS} players to start.")
        if not room.remaining_words:
            raise GameError("Add some words (or recycle used ones) first.")
        if room.num_imposters >= len(seated):
            raise GameError("Imposters must be fewer than the number of players.")
        if room.num_imposters < 1:
            raise GameError("You need at least one imposter.")

        word = room.remaining_words.pop(self.rng.randrange(len(room.remaining_words)))
        room.used_words.append(word)
        imposters = [p.id for p in self.rng.sample(seated, room.num_imposters)]
        order = [p.id for p in seated]
        self.rng.shuffle(order)
        clock = time.time() if now is None else now
        round_state = RoundState(
            word=word,
            clue=clue,
            imposter_ids=imposters,
            participant_ids=[p.id for p in seated],
            speaking_order=order,
        )
        room.round = round_state
        room.round_number += 1
        room.phase = "reveal"
        self._touch(room, at=clock)
        return round_state

    def mark_ready(self, room: Room, player: Player) -> None:
        self.tick(room)
        if room.phase != "reveal" or room.round is None:
            raise GameError("Nothing to confirm right now.")
        if player.id not in room.round.participant_ids:
            raise GameError("You will join on the next round.")
        room.round.ready_ids.add(player.id)
        self._touch(room)
        seated = [pid for pid in room.round.participant_ids if pid in room.players]
        if seated and set(seated).issubset(room.round.ready_ids):
            self._enter_discuss(room)

    def peek_role(self, room: Room, host: Player, player_id: str) -> dict[str, Any]:
        """Pass-and-play: host reveals one player's card on a shared phone."""
        self._require_host(host)
        if not room.pass_and_play:
            raise GameError("Turn on pass-and-play to reveal on one phone.")
        if room.phase != "reveal" or room.round is None:
            raise GameError("Reveal is not open.")
        if player_id not in room.round.participant_ids:
            raise GameError("That player is sitting this round out.")
        target = room.players.get(player_id)
        if target is None:
            raise GameError("That player left.")
        room.round.ready_ids.add(player_id)
        self._touch(room)
        seated = [pid for pid in room.round.participant_ids if pid in room.players]
        if seated and set(seated).issubset(room.round.ready_ids):
            self._enter_discuss(room)
        return self._role_payload(room, room.round, player_id, target.name)

    def next_speaker(self, room: Room, host: Player) -> None:
        self._require_host(host)
        self.tick(room)
        if room.phase != "discuss" or room.round is None:
            raise GameError("Speaking order is only during the circle.")
        last = max(len(room.round.speaking_order) - 1, 0)
        if room.round.speaker_index >= last:
            raise GameError("That was the last person. Go around again or open the floor.")
        room.round.speaker_index += 1
        self._touch(room)

    def go_around_again(self, room: Room, host: Player) -> None:
        self._require_host(host)
        self.tick(room)
        if room.phase != "discuss" or room.round is None:
            raise GameError("You can only go around again during the circle.")
        order = list(room.round.speaking_order)
        if len(order) > 1:
            order = order[1:] + order[:1]
        room.round.speaking_order = order
        room.round.speaker_index = 0
        room.round.lap += 1
        self._touch(room)

    def guess_word(self, room: Room, player: Player, word: str | None) -> None:
        """Imposters get one private shot at the word after the open floor."""
        self.tick(room)
        rnd = room.round
        if room.phase != "guess" or rnd is None:
            raise GameError("Guessing is not open yet.")
        if player.id not in rnd.participant_ids:
            raise GameError("You will play next round.")
        if player.id not in rnd.imposter_ids:
            raise GameError("Only imposters can guess the word.")
        if player.id in rnd.guesses:
            raise GameError("You already used your guess.")
        cleaned = " ".join((word or "").split())
        rnd.guesses[player.id] = cleaned
        self._touch(room)
        if cleaned and _normalize_guess(cleaned) == _normalize_guess(rnd.word):
            self._resolve_guess_win(room, player.id)
            return
        seated = self._seated_imposters(room)
        if seated and all(pid in rnd.guesses for pid in seated):
            self._enter_vote(room)

    def advance(self, room: Room, host: Player, *, now: float | None = None) -> None:
        """Host skips ahead: reveal → discuss → huddle → guess → vote → results."""
        self._require_host(host)
        self.tick(room, now=now)
        if room.phase == "reveal":
            self._enter_discuss(room, now=now)
        elif room.phase == "discuss":
            self._enter_huddle(room, now=now)
        elif room.phase == "huddle":
            self._enter_guess(room, now=now)
        elif room.phase == "guess":
            self._enter_vote(room)
        elif room.phase == "vote":
            self._resolve_votes(room)
        else:
            raise GameError("Nothing to advance.")

    def vote(self, room: Room, player: Player, target_id: str | None) -> None:
        self.tick(room)
        if room.phase != "vote" or room.round is None:
            raise GameError("Voting is not open.")
        if player.id not in room.round.participant_ids:
            raise GameError("You will vote next round.")
        if target_id == player.id:
            raise GameError("You cannot vote for yourself.")
        if target_id is not None and target_id not in room.round.participant_ids:
            raise GameError("That is not a player in this round.")
        room.round.votes[player.id] = target_id
        self._touch(room)
        seated = [pid for pid in room.round.participant_ids if pid in room.players]
        if seated and all(pid in room.round.votes for pid in seated):
            self._resolve_votes(room)

    def tick(self, room: Room, now: float | None = None) -> None:
        clock = time.time() if now is None else now
        if (
            room.phase == "huddle"
            and room.round
            and room.round.discuss_ends_at is not None
            and clock >= room.round.discuss_ends_at
        ):
            self._enter_guess(room, now=clock)

    def view_for(self, room: Room, player: Player, *, now: float | None = None) -> dict[str, Any]:
        self.tick(room, now=now)
        rnd = room.round
        participants = set(rnd.participant_ids) if rnd else set()
        sitting_out = bool(rnd and player.id not in participants)

        you: dict[str, Any] = {
            "id": player.id,
            "name": player.name,
            "isHost": player.is_host,
            "score": player.score,
            "sittingOut": sitting_out,
            "ready": bool(rnd and player.id in rnd.ready_ids),
            "votedFor": rnd.votes.get(player.id) if rnd else None,
            "hasVoted": bool(rnd and player.id in rnd.votes),
            "canGuess": bool(
                rnd
                and room.phase == "guess"
                and not sitting_out
                and player.id in rnd.imposter_ids
                and player.id not in rnd.guesses
            ),
            "hasGuessed": bool(rnd and player.id in rnd.guesses),
            "guessMissed": bool(
                rnd
                and player.id in rnd.guesses
                and rnd.guesses.get(player.id)
                and rnd.winner is None
            ),
        }
        live_phases = ("reveal", "discuss", "huddle", "guess", "vote")
        if rnd and not sitting_out and room.phase in live_phases:
            you["role"] = self._role_payload(room, rnd, player.id, player.name)
        elif rnd and room.phase in ("results", "ended"):
            you["role"] = self._role_payload(room, rnd, player.id, player.name)

        players = []
        for p in room.players.values():
            entry: dict[str, Any] = {
                "id": p.id,
                "name": p.name,
                "isHost": p.is_host,
                "score": p.score,
                "ready": bool(rnd and p.id in rnd.ready_ids),
                "hasVoted": bool(rnd and p.id in rnd.votes),
                "inRound": bool(not rnd or p.id in participants),
            }
            if rnd and room.phase in ("results", "ended"):
                entry["wasImposter"] = p.id in rnd.imposter_ids
                entry["votedFor"] = rnd.votes.get(p.id)
                entry["scoreDelta"] = rnd.score_delta.get(p.id, 0)
            players.append(entry)
        players.sort(key=lambda row: (-row["score"], row["name"].casefold()))

        speaking = []
        if rnd:
            for pid in rnd.speaking_order:
                person = room.players.get(pid)
                if person:
                    speaking.append({"id": pid, "name": person.name})

        payload: dict[str, Any] = {
            "code": room.code,
            "phase": room.phase,
            "roundNumber": room.round_number,
            "numImposters": room.num_imposters,
            "discussSeconds": room.discuss_seconds,
            "passAndPlay": room.pass_and_play,
            "wordsVisible": room.words_visible and player.is_host,
            "remainingWordCount": len(room.remaining_words),
            "usedWordCount": len(room.used_words),
            "canStart": self._can_start(room),
            "you": you,
            "players": players,
            "speakingOrder": speaking,
            "speakerIndex": rnd.speaker_index if rnd else 0,
            "speakerLap": rnd.lap if rnd else 1,
            "discussEndsAt": rnd.discuss_ends_at if rnd else None,
            "updatedAt": room.updated_at,
        }
        if player.is_host:
            payload["words"] = list(room.remaining_words) if room.words_visible else []
            payload["usedWords"] = list(room.used_words) if room.words_visible else []
            payload["invites"] = [
                {
                    "token": inv.token,
                    "name": inv.name,
                    "phone": inv.phone,
                    "claimed": bool(inv.claimed_by and inv.claimed_by in room.players),
                }
                for inv in room.invites.values()
            ]
        if rnd and room.phase in ("results", "ended"):
            payload["result"] = {
                "word": rnd.word,
                "clue": rnd.clue,
                "winner": rnd.winner,
                "winReason": rnd.win_reason,
                "guessedBy": rnd.guessed_by,
                "imposterIds": list(rnd.imposter_ids),
                "eliminatedIds": list(rnd.eliminated_ids),
                "voteCounts": dict(rnd.vote_counts),
                "scoreDelta": dict(rnd.score_delta),
            }
        return payload

    def sweep_idle(self, now: float | None = None) -> None:
        clock = time.time() if now is None else now
        stale = [
            code
            for code, room in self.rooms.items()
            if clock - room.updated_at > ROOM_IDLE_SECONDS
        ]
        for code in stale:
            room = self.rooms.pop(code)
            for player in room.players.values():
                self.token_index.pop(player.token, None)
            for token in room.invites:
                self.invite_index.pop(token, None)

    def _enter_discuss(self, room: Room, now: float | None = None) -> None:
        if room.round is None:
            return
        clock = time.time() if now is None else now
        room.phase = "discuss"
        room.round.discuss_ends_at = None
        if room.round.lap < 1:
            room.round.lap = 1
        self._touch(room, at=clock)

    def _enter_huddle(self, room: Room, now: float | None = None) -> None:
        if room.round is None:
            return
        clock = time.time() if now is None else now
        room.phase = "huddle"
        if room.discuss_seconds > 0:
            room.round.discuss_ends_at = clock + room.discuss_seconds
        else:
            room.round.discuss_ends_at = None
        self._touch(room, at=clock)

    def _enter_guess(self, room: Room, now: float | None = None) -> None:
        if room.round is None:
            return
        if room.pass_and_play:
            self._enter_vote(room)
            return
        clock = time.time() if now is None else now
        room.phase = "guess"
        room.round.discuss_ends_at = None
        self._touch(room, at=clock)

    def _enter_vote(self, room: Room) -> None:
        if room.round is None:
            return
        room.round.clue_unlocked = True
        room.round.discuss_ends_at = None
        room.phase = "vote"
        self._touch(room)

    def _resolve_guess_win(self, room: Room, guesser_id: str) -> None:
        rnd = room.round
        if rnd is None:
            return
        rnd.winner = "imposters"
        rnd.win_reason = "guess"
        rnd.guessed_by = guesser_id
        rnd.clue_unlocked = True
        delta: dict[str, int] = {}
        for pid in rnd.participant_ids:
            delta[pid] = 4 if pid in rnd.imposter_ids else 0
            player = room.players.get(pid)
            if player:
                player.score += delta[pid]
        rnd.score_delta = delta
        room.phase = "results"
        self._touch(room)

    def _resolve_votes(self, room: Room) -> None:
        rnd = room.round
        if rnd is None:
            return
        counts = Counter(target for target in rnd.votes.values() if target)
        rnd.vote_counts = dict(counts)
        eliminated: list[str] = []
        if counts:
            top = max(counts.values())
            eliminated = [pid for pid, n in counts.items() if n == top]
        rnd.eliminated_ids = eliminated
        imposters = set(rnd.imposter_ids)
        if len(eliminated) == 1 and eliminated[0] in imposters:
            rnd.winner = "faithfuls"
        else:
            rnd.winner = "imposters"
        rnd.win_reason = "vote"

        delta: dict[str, int] = {}
        for pid in rnd.participant_ids:
            voted = rnd.votes.get(pid)
            voted_imposter = voted in imposters
            if pid in imposters:
                delta[pid] = 3 if rnd.winner == "imposters" else 0
            else:
                gained = 0
                if rnd.winner == "faithfuls":
                    gained += 2
                if voted_imposter:
                    gained += 1
                delta[pid] = gained
            player = room.players.get(pid)
            if player:
                player.score += delta[pid]
        rnd.score_delta = delta
        room.phase = "results"
        self._touch(room)

    def _remove_player(self, room: Room, player: Player) -> None:
        room.players.pop(player.id, None)
        self.token_index.pop(player.token, None)
        if player.is_host and room.players:
            successor = next(iter(room.players.values()))
            successor.is_host = True
        if not room.players:
            self.rooms.pop(room.code, None)
            return
        self._touch(room)

    def _can_start(self, room: Room) -> bool:
        return (
            room.phase in ("lobby", "results")
            and len(room.players) >= MIN_PLAYERS
            and len(room.remaining_words) >= 1
            and 1 <= room.num_imposters < len(room.players)
        )

    def _participants(self, room: Room) -> set[str]:
        if room.round is None:
            return set()
        return set(room.round.participant_ids)

    def _seated_imposters(self, room: Room) -> list[str]:
        rnd = room.round
        if rnd is None:
            return []
        return [pid for pid in rnd.imposter_ids if pid in room.players]

    def _role_payload(self, room: Room, rnd: RoundState, player_id: str, name: str) -> dict[str, Any]:
        if player_id in rnd.imposter_ids:
            show_clue = bool(
                rnd.clue
                and (
                    room.pass_and_play
                    or rnd.clue_unlocked
                    or room.phase in ("results", "ended")
                )
            )
            return {
                "kind": "imposter",
                "name": name,
                "clue": rnd.clue if show_clue else None,
                "word": None,
            }
        return {
            "kind": "faithful",
            "name": name,
            "clue": None,
            "word": rnd.word,
        }

    def _require_host(self, player: Player) -> None:
        if not player.is_host:
            raise GameError("Only the host can do that.", 403)

    def _room(self, code: str) -> Room:
        key = (code or "").strip().upper()
        room = self.rooms.get(key)
        if room is None:
            raise GameError("No room with that code.", 404)
        return room

    def _unique_code(self) -> str:
        for _ in range(50):
            code = "".join(self.rng.choice(CODE_ALPHABET) for _ in range(4))
            if code not in self.rooms:
                return code
        raise GameError("Could not create a room. Try again.", 500)

    def _touch(self, room: Room, at: float | None = None) -> None:
        room.updated_at = time.time() if at is None else at

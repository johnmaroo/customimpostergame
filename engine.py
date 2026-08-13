"""In-memory party sessions for Imposter.

The web UI and tests both drive this module. Secrets (the round's word and
who the imposters are) stay on the server and are only placed in a player's
own view — never in the shared room snapshot.
"""

from __future__ import annotations

import json
import random
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from prompts import IRL_MODES, pick_prompt, prompt_view

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"
MIN_PLAYERS = 3
MAX_NAME_LEN = 24
MAX_WORD_LEN = 48
MAX_WORDS_PER_ROOM = 200
ROOM_IDLE_SECONDS = 4 * 60 * 60
USED_PROMPT_CAP = 20

Phase = Literal["lobby", "reveal", "discuss", "vote", "results", "ended"]
Winner = Literal["faithfuls", "imposters"]
IrlMode = Literal["off", "mix", "ask", "do"]


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


def _sample_by_fewest(
    rng: random.Random,
    player_ids: list[str],
    counts: dict[str, int],
    k: int,
    avoid: list[str] | None = None,
) -> list[str]:
    """Pick k players, filling from those chosen the fewest times.

    Within a count bucket, recently chosen ids are skipped when enough
    other people are tied with them. That keeps deals random among the
    people who are due, without locking in a fixed rotation.
    """
    if k < 0 or k > len(player_ids):
        raise ValueError("cannot sample that many players")
    avoid_set = set(avoid or [])
    remaining = list(player_ids)
    picked: list[str] = []
    while len(picked) < k:
        min_c = min(counts.get(pid, 0) for pid in remaining)
        pool = [pid for pid in remaining if counts.get(pid, 0) == min_c]
        preferred = [pid for pid in pool if pid not in avoid_set]
        use = preferred if preferred else pool
        take = min(k - len(picked), len(use))
        chosen = rng.sample(use, take)
        picked.extend(chosen)
        chosen_set = set(chosen)
        remaining = [pid for pid in remaining if pid not in chosen_set]
    return picked


def _shuffled_copy(
    rng: random.Random,
    items: list[str],
    *,
    unlike: list[str] | None = None,
) -> list[str]:
    items = list(items)
    if len(items) <= 1:
        return items
    result = list(items)
    for _ in range(12):
        rng.shuffle(items)
        if unlike is None or items != unlike:
            return list(items)
        result = list(items)
    return result


def _deal_speaking_order(
    rng: random.Random,
    player_ids: list[str],
    starter_counts: dict[str, int],
    previous: list[str] | None,
    last_starter: str | None,
) -> list[str]:
    ids = list(player_ids)
    avoid = [last_starter] if last_starter else []
    starter = _sample_by_fewest(rng, ids, starter_counts, 1, avoid=avoid)[0]
    rest = [pid for pid in ids if pid != starter]
    unlike = None
    if previous and previous[:1] == [starter] and set(previous[1:]) == set(rest):
        unlike = previous[1:]
    return [starter] + _shuffled_copy(rng, rest, unlike=unlike)


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
    ready_ids: set[str] = field(default_factory=set)
    votes: dict[str, str | None] = field(default_factory=dict)
    discuss_ends_at: float | None = None
    winner: Winner | None = None
    eliminated_ids: list[str] = field(default_factory=list)
    score_delta: dict[str, int] = field(default_factory=dict)
    vote_counts: dict[str, int] = field(default_factory=dict)
    prompt: dict[str, str] | None = None


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
    irl_mode: IrlMode = "mix"
    imposter_hints: bool = True
    phase: Phase = "lobby"
    round: RoundState | None = None
    round_number: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    invites: dict[str, Invite] = field(default_factory=dict)
    word_sources: dict[str, str] = field(default_factory=dict)
    used_prompt_ids: list[str] = field(default_factory=list)
    imposter_counts: dict[str, int] = field(default_factory=dict)
    starter_counts: dict[str, int] = field(default_factory=dict)
    last_imposter_ids: list[str] = field(default_factory=list)
    last_speaking_order: list[str] = field(default_factory=list)


class GameHub:
    def __init__(
        self,
        rng: random.Random | None = None,
        persist_path: str | Path | None = None,
    ) -> None:
        self.rooms: dict[str, Room] = {}
        self.token_index: dict[str, tuple[str, str]] = {}
        self.invite_index: dict[str, str] = {}
        self.rng = rng if rng is not None else random.SystemRandom()
        self.persist_path = Path(persist_path) if persist_path else None
        self.restore()

    def create_room(self, host_name: str) -> tuple[Room, Player]:
        name = _clean_name(host_name)
        code = self._unique_code()
        host = Player(id=_new_id(), name=name, token=_new_token(), is_host=True)
        room = Room(code=code, players={host.id: host})
        self.rooms[code] = room
        self.token_index[host.token] = (code, host.id)
        self._touch(room)
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
            claimed = room.players.get(invite.claimed_by or "")
            if claimed:
                return self._reclaim_player(room, claimed)
        cleaned = _clean_name(name or (invite.name if invite else ""))
        existing = next(
            (p for p in room.players.values() if p.name.casefold() == cleaned.casefold()),
            None,
        )
        if existing:
            return self._reclaim_player(room, existing)
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

    def _reclaim_player(self, room: Room, player: Player) -> tuple[Room, Player]:
        self.token_index.pop(player.token, None)
        player.token = _new_token()
        player.connected = True
        self.token_index[player.token] = (room.code, player.id)
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
            raise GameError("Session expired. Join again with the same name.", 401)
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
        irl_mode: str | None = None,
        imposter_hints: bool | None = None,
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
        if irl_mode is not None:
            cleaned_mode = irl_mode.strip().lower()
            if cleaned_mode not in IRL_MODES:
                raise GameError("Pick an IRL turn style from the list.")
            room.irl_mode = cleaned_mode  # type: ignore[assignment]
        if imposter_hints is not None:
            room.imposter_hints = bool(imposter_hints)
        self._touch(room)

    def add_word(self, room: Room, player: Player, word: str, *, source: str | None = None) -> str:
        if room.phase not in ("lobby", "results"):
            raise GameError("Add words between rounds.")
        cleaned = _clean_word(word)
        if len(room.remaining_words) + len(room.used_words) >= MAX_WORDS_PER_ROOM:
            raise GameError("Word bank is full.")
        already = {item.casefold() for item in room.remaining_words + room.used_words}
        if cleaned.casefold() in already:
            if source:
                room.word_sources.setdefault(cleaned.casefold(), source)
            if cleaned.casefold() in {item.casefold() for item in room.remaining_words}:
                return cleaned
            room.used_words = [w for w in room.used_words if w.casefold() != cleaned.casefold()]
            room.remaining_words.append(cleaned)
            self._touch(room)
            return cleaned
        room.remaining_words.append(cleaned)
        if source:
            room.word_sources[cleaned.casefold()] = source
        self._touch(room)
        return cleaned

    def add_words(self, room: Room, host: Player, words: list[str]) -> list[str]:
        self._require_host(host)
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
        room.word_sources.pop(target.casefold(), None)
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
        seated_ids = [p.id for p in seated]
        imposters = _sample_by_fewest(
            self.rng,
            seated_ids,
            room.imposter_counts,
            room.num_imposters,
            avoid=room.last_imposter_ids,
        )
        self.rng.shuffle(imposters)
        previous_order = room.last_speaking_order or seated_ids
        last_starter = room.last_speaking_order[0] if room.last_speaking_order else None
        order = _deal_speaking_order(
            self.rng,
            seated_ids,
            room.starter_counts,
            previous=previous_order,
            last_starter=last_starter,
        )
        for pid in imposters:
            room.imposter_counts[pid] = room.imposter_counts.get(pid, 0) + 1
        room.starter_counts[order[0]] = room.starter_counts.get(order[0], 0) + 1
        room.last_imposter_ids = list(imposters)
        room.last_speaking_order = list(order)
        clock = time.time() if now is None else now
        round_state = RoundState(
            word=word,
            clue=clue,
            imposter_ids=imposters,
            participant_ids=[p.id for p in seated],
            speaking_order=order,
            prompt=self._deal_prompt(room, word),
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
            raise GameError("Speaking order is only during discussion.")
        room.round.speaker_index = min(
            room.round.speaker_index + 1,
            max(len(room.round.speaking_order) - 1, 0),
        )
        self._touch(room)

    def next_prompt(self, room: Room, host: Player) -> dict[str, str] | None:
        """Host swaps the shared IRL prompt during reveal or discussion."""
        self._require_host(host)
        self.tick(room)
        if room.phase not in ("reveal", "discuss") or room.round is None:
            raise GameError("IRL prompts can change during reveal or discussion.")
        if room.irl_mode == "off":
            raise GameError("Turn on IRL turns to deal a prompt.")
        prompt = self._deal_prompt(room, room.round.word, extra_exclude={
            room.round.prompt["id"] if room.round.prompt else ""
        })
        room.round.prompt = prompt
        self._touch(room)
        return prompt

    def advance(self, room: Room, host: Player, *, now: float | None = None) -> None:
        """Host skips ahead: reveal → discuss → vote → results."""
        self._require_host(host)
        self.tick(room, now=now)
        if room.phase == "reveal":
            self._enter_discuss(room, now=now)
        elif room.phase == "discuss":
            room.phase = "vote"
            self._touch(room)
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
            room.phase == "discuss"
            and room.round
            and room.round.discuss_ends_at is not None
            and clock >= room.round.discuss_ends_at
        ):
            room.phase = "vote"
            self._touch(room, at=clock)

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
        }
        if rnd and not sitting_out and room.phase in ("reveal", "discuss", "vote"):
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
            "irlMode": room.irl_mode,
            "imposterHints": room.imposter_hints,
            "prompt": rnd.prompt if rnd else None,
            "remainingWordCount": len(room.remaining_words),
            "usedWordCount": len(room.used_words),
            "canStart": self._can_start(room),
            "you": you,
            "players": players,
            "speakingOrder": speaking,
            "speakerIndex": rnd.speaker_index if rnd else 0,
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
        if stale:
            self.persist()

    def _deal_prompt(
        self,
        room: Room,
        word: str,
        *,
        extra_exclude: set[str] | None = None,
    ) -> dict[str, str] | None:
        blocked = [pid for pid in room.used_prompt_ids if pid]
        if extra_exclude:
            blocked.extend(item for item in extra_exclude if item)
        picked = pick_prompt(
            self.rng,
            mode=room.irl_mode,
            pack_id=room.word_sources.get(word.casefold()),
            exclude=blocked,
        )
        view = prompt_view(picked)
        if view:
            room.used_prompt_ids.append(view["id"])
            room.used_prompt_ids = room.used_prompt_ids[-USED_PROMPT_CAP:]
        return view

    def _enter_discuss(self, room: Room, now: float | None = None) -> None:
        if room.round is None:
            return
        clock = time.time() if now is None else now
        room.phase = "discuss"
        if room.discuss_seconds > 0:
            room.round.discuss_ends_at = clock + room.discuss_seconds
        else:
            room.round.discuss_ends_at = None
        self._touch(room, at=clock)

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
        room.imposter_counts.pop(player.id, None)
        room.starter_counts.pop(player.id, None)
        if player.is_host and room.players:
            successor = next(iter(room.players.values()))
            successor.is_host = True
        if not room.players:
            self.rooms.pop(room.code, None)
            self.persist()
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

    def _role_payload(self, room: Room, rnd: RoundState, player_id: str, name: str) -> dict[str, Any]:
        if player_id in rnd.imposter_ids:
            show_clue = bool(rnd.clue) and (
                room.imposter_hints or room.phase in ("results", "ended")
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

    def persist(self) -> None:
        if not self.persist_path:
            return
        payload = {"rooms": [self._dump_room(room) for room in self.rooms.values()]}
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.persist_path.with_name(self.persist_path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.persist_path)

    def restore(self) -> None:
        if not self.persist_path or not self.persist_path.exists():
            return
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.rooms = {}
        self.token_index = {}
        self.invite_index = {}
        for raw in payload.get("rooms") or []:
            try:
                room = self._load_room(raw)
            except (KeyError, TypeError, ValueError):
                continue
            self.rooms[room.code] = room
            for player in room.players.values():
                self.token_index[player.token] = (room.code, player.id)
            for token in room.invites:
                self.invite_index[token] = room.code

    def _dump_room(self, room: Room) -> dict[str, Any]:
        rnd = room.round
        return {
            "code": room.code,
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "token": p.token,
                    "isHost": p.is_host,
                    "score": p.score,
                    "connected": p.connected,
                    "phone": p.phone,
                }
                for p in room.players.values()
            ],
            "remainingWords": list(room.remaining_words),
            "usedWords": list(room.used_words),
            "numImposters": room.num_imposters,
            "discussSeconds": room.discuss_seconds,
            "passAndPlay": room.pass_and_play,
            "wordsVisible": room.words_visible,
            "irlMode": room.irl_mode,
            "imposterHints": room.imposter_hints,
            "phase": room.phase,
            "roundNumber": room.round_number,
            "createdAt": room.created_at,
            "updatedAt": room.updated_at,
            "invites": [
                {
                    "token": inv.token,
                    "name": inv.name,
                    "phone": inv.phone,
                    "claimedBy": inv.claimed_by,
                }
                for inv in room.invites.values()
            ],
            "wordSources": dict(room.word_sources),
            "usedPromptIds": list(room.used_prompt_ids),
            "imposterCounts": dict(room.imposter_counts),
            "starterCounts": dict(room.starter_counts),
            "lastImposterIds": list(room.last_imposter_ids),
            "lastSpeakingOrder": list(room.last_speaking_order),
            "round": None
            if rnd is None
            else {
                "word": rnd.word,
                "clue": rnd.clue,
                "imposterIds": list(rnd.imposter_ids),
                "participantIds": list(rnd.participant_ids),
                "speakingOrder": list(rnd.speaking_order),
                "speakerIndex": rnd.speaker_index,
                "readyIds": list(rnd.ready_ids),
                "votes": dict(rnd.votes),
                "discussEndsAt": rnd.discuss_ends_at,
                "winner": rnd.winner,
                "eliminatedIds": list(rnd.eliminated_ids),
                "scoreDelta": dict(rnd.score_delta),
                "voteCounts": dict(rnd.vote_counts),
                "prompt": rnd.prompt,
            },
        }

    def _load_room(self, raw: dict[str, Any]) -> Room:
        players = {
            row["id"]: Player(
                id=row["id"],
                name=row["name"],
                token=row["token"],
                is_host=bool(row.get("isHost")),
                score=int(row.get("score") or 0),
                connected=bool(row.get("connected", True)),
                phone=row.get("phone"),
            )
            for row in raw.get("players") or []
        }
        invites = {
            row["token"]: Invite(
                token=row["token"],
                name=row["name"],
                phone=row["phone"],
                claimed_by=row.get("claimedBy"),
            )
            for row in raw.get("invites") or []
        }
        rnd_raw = raw.get("round")
        rnd = None
        if rnd_raw:
            rnd = RoundState(
                word=rnd_raw["word"],
                clue=rnd_raw.get("clue"),
                imposter_ids=list(rnd_raw.get("imposterIds") or []),
                participant_ids=list(rnd_raw.get("participantIds") or []),
                speaking_order=list(rnd_raw.get("speakingOrder") or []),
                speaker_index=int(rnd_raw.get("speakerIndex") or 0),
                ready_ids=set(rnd_raw.get("readyIds") or []),
                votes=dict(rnd_raw.get("votes") or {}),
                discuss_ends_at=rnd_raw.get("discussEndsAt"),
                winner=rnd_raw.get("winner"),
                eliminated_ids=list(rnd_raw.get("eliminatedIds") or []),
                score_delta=dict(rnd_raw.get("scoreDelta") or {}),
                vote_counts=dict(rnd_raw.get("voteCounts") or {}),
                prompt=rnd_raw.get("prompt"),
            )
        return Room(
            code=str(raw["code"]).upper(),
            players=players,
            remaining_words=list(raw.get("remainingWords") or []),
            used_words=list(raw.get("usedWords") or []),
            num_imposters=int(raw.get("numImposters") or 1),
            discuss_seconds=int(raw.get("discussSeconds") or 90),
            pass_and_play=bool(raw.get("passAndPlay")),
            words_visible=bool(raw.get("wordsVisible")),
            irl_mode=raw.get("irlMode") or "mix",
            imposter_hints=bool(raw.get("imposterHints", True)),
            phase=raw.get("phase") or "lobby",
            round=rnd,
            round_number=int(raw.get("roundNumber") or 0),
            created_at=float(raw.get("createdAt") or time.time()),
            updated_at=float(raw.get("updatedAt") or time.time()),
            invites=invites,
            word_sources=dict(raw.get("wordSources") or {}),
            used_prompt_ids=list(raw.get("usedPromptIds") or []),
            imposter_counts=dict(raw.get("imposterCounts") or {}),
            starter_counts=dict(raw.get("starterCounts") or {}),
            last_imposter_ids=list(raw.get("lastImposterIds") or []),
            last_speaking_order=list(raw.get("lastSpeakingOrder") or []),
        )

    def _touch(self, room: Room, at: float | None = None) -> None:
        room.updated_at = time.time() if at is None else at
        self.persist()

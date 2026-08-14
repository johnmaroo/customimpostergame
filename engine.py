"""Party sessions for Imposter.

The web UI and tests both drive this module. Secrets (the round's word and
who the imposters are) stay on the server and are only placed in a player's
own view — never in the shared room snapshot.

Rooms live in a `RoomStore` rather than in module state, so a table survives a
server restart and can be picked up by whichever process handles the next
request. Session tokens carry the room code and player id, which lets any
process re-attach a phone by loading that one room.
"""

from __future__ import annotations

import hashlib
import random
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field, fields
from typing import Any, Literal, Protocol

from prompts import PROMPT_MODES, pick_prompt, prompt_view

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"
MIN_PLAYERS = 3
MAX_NAME_LEN = 24
MAX_WORD_LEN = 48
MAX_WORDS_PER_ROOM = 200
ROOM_IDLE_SECONDS = 4 * 60 * 60
USED_PROMPT_CAP = 20
ROOM_SCHEMA_VERSION = 1
# A phone polling every couple of seconds should keep its table alive without
# writing to the store on every request.
ACTIVITY_WRITE_SECONDS = 60.0
TOKEN_SEPARATOR = "."

Phase = Literal["lobby", "reveal", "discuss", "huddle", "guess", "vote", "results", "ended"]
Winner = Literal["faithfuls", "imposters"]
WinReason = Literal["guess", "vote"]
IrlMode = Literal["off", "classic", "mix", "ask", "do"]


class GameError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "game_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class StoreConflict(Exception):
    """Another writer changed this room first; the caller should retry."""


class StoreUnavailable(Exception):
    """The room store could not be reached. The table is not lost, just unreadable."""


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


def hash_token(token: str) -> str:
    """Rooms are written to disk or a shared cache, so only hashes are stored."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _session_token(code: str, player_id: str) -> str:
    """A token that says which room and seat it belongs to, plus a secret."""
    return TOKEN_SEPARATOR.join((code, player_id, secrets.token_urlsafe(24)))


def _read_session_token(token: str) -> tuple[str, str] | None:
    parts = (token or "").strip().split(TOKEN_SEPARATOR)
    if len(parts) != 3 or not all(parts):
        return None
    code, player_id, _secret = parts
    return code.upper(), player_id


def _seat(code: str, name: str, *, is_host: bool, phone: str | None = None) -> Player:
    player_id = _new_id()
    return Player(
        id=player_id,
        name=name,
        token=_session_token(code, player_id),
        is_host=is_host,
        phone=phone,
    )


def _invite_token(code: str) -> str:
    return TOKEN_SEPARATOR.join((code, secrets.token_urlsafe(18)))


def _read_invite_token(token: str) -> str | None:
    parts = (token or "").strip().split(TOKEN_SEPARATOR)
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0].upper()


@dataclass
class Player:
    id: str
    name: str
    token: str
    is_host: bool
    score: int = 0
    connected: bool = True
    phone: str | None = None
    token_hash: str = ""
    last_seen: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.token_hash and self.token:
            self.token_hash = hash_token(self.token)


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
    last_seen_at: float = field(default_factory=time.time)
    version: int = 0


def round_to_dict(rnd: RoundState) -> dict[str, Any]:
    return {
        "word": rnd.word,
        "clue": rnd.clue,
        "imposterIds": list(rnd.imposter_ids),
        "participantIds": list(rnd.participant_ids),
        "speakingOrder": list(rnd.speaking_order),
        "speakerIndex": rnd.speaker_index,
        "lap": rnd.lap,
        "readyIds": sorted(rnd.ready_ids),
        "votes": dict(rnd.votes),
        "guesses": dict(rnd.guesses),
        "discussEndsAt": rnd.discuss_ends_at,
        "winner": rnd.winner,
        "winReason": rnd.win_reason,
        "guessedBy": rnd.guessed_by,
        "clueUnlocked": rnd.clue_unlocked,
        "eliminatedIds": list(rnd.eliminated_ids),
        "scoreDelta": dict(rnd.score_delta),
        "voteCounts": dict(rnd.vote_counts),
        "prompt": dict(rnd.prompt) if rnd.prompt else None,
    }


def round_from_dict(data: dict[str, Any]) -> RoundState:
    return RoundState(
        word=data["word"],
        clue=data.get("clue"),
        imposter_ids=list(data.get("imposterIds") or []),
        participant_ids=list(data.get("participantIds") or []),
        speaking_order=list(data.get("speakingOrder") or []),
        speaker_index=int(data.get("speakerIndex") or 0),
        lap=int(data.get("lap") or 1),
        ready_ids=set(data.get("readyIds") or []),
        votes=dict(data.get("votes") or {}),
        guesses=dict(data.get("guesses") or {}),
        discuss_ends_at=data.get("discussEndsAt"),
        winner=data.get("winner"),
        win_reason=data.get("winReason"),
        guessed_by=data.get("guessedBy"),
        clue_unlocked=bool(data.get("clueUnlocked")),
        eliminated_ids=list(data.get("eliminatedIds") or []),
        score_delta=dict(data.get("scoreDelta") or {}),
        vote_counts=dict(data.get("voteCounts") or {}),
        prompt=dict(data["prompt"]) if data.get("prompt") else None,
    )


def room_to_dict(room: Room) -> dict[str, Any]:
    """JSON-ready room. Bearer tokens are replaced by their hashes."""
    return {
        "schema": ROOM_SCHEMA_VERSION,
        "code": room.code,
        "players": [
            {
                "id": p.id,
                "name": p.name,
                "tokenHash": p.token_hash,
                "isHost": p.is_host,
                "score": p.score,
                "connected": p.connected,
                "phone": p.phone,
                "lastSeen": p.last_seen,
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
        "round": round_to_dict(room.round) if room.round else None,
        "roundNumber": room.round_number,
        "createdAt": room.created_at,
        "updatedAt": room.updated_at,
        "lastSeenAt": room.last_seen_at,
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
        "version": room.version,
    }


def room_from_dict(data: dict[str, Any]) -> Room:
    players: dict[str, Player] = {}
    for row in data.get("players") or []:
        player = Player(
            id=row["id"],
            name=row["name"],
            token="",
            is_host=bool(row.get("isHost")),
            score=int(row.get("score") or 0),
            connected=bool(row.get("connected")),
            phone=row.get("phone"),
            token_hash=row.get("tokenHash") or "",
            last_seen=float(row.get("lastSeen") or 0.0),
        )
        players[player.id] = player
    invites: dict[str, Invite] = {}
    for row in data.get("invites") or []:
        invites[row["token"]] = Invite(
            token=row["token"],
            name=row["name"],
            phone=row["phone"],
            claimed_by=row.get("claimedBy"),
        )
    created = float(data.get("createdAt") or time.time())
    updated = float(data.get("updatedAt") or created)
    return Room(
        code=data["code"],
        players=players,
        remaining_words=list(data.get("remainingWords") or []),
        used_words=list(data.get("usedWords") or []),
        num_imposters=int(data.get("numImposters") or 1),
        discuss_seconds=int(data.get("discussSeconds") or 0),
        pass_and_play=bool(data.get("passAndPlay")),
        words_visible=bool(data.get("wordsVisible")),
        irl_mode=data.get("irlMode") or "mix",
        imposter_hints=bool(data.get("imposterHints")),
        phase=data.get("phase") or "lobby",
        round=round_from_dict(data["round"]) if data.get("round") else None,
        round_number=int(data.get("roundNumber") or 0),
        created_at=created,
        updated_at=updated,
        invites=invites,
        word_sources=dict(data.get("wordSources") or {}),
        used_prompt_ids=list(data.get("usedPromptIds") or []),
        imposter_counts=dict(data.get("imposterCounts") or {}),
        starter_counts=dict(data.get("starterCounts") or {}),
        last_imposter_ids=list(data.get("lastImposterIds") or []),
        last_speaking_order=list(data.get("lastSpeakingOrder") or []),
        last_seen_at=float(data.get("lastSeenAt") or updated),
        version=int(data.get("version") or 0),
    )


def serialized_round_fields() -> set[str]:
    """Field names covered by `round_to_dict`, so new state cannot slip through."""
    return {
        "word",
        "clue",
        "imposter_ids",
        "participant_ids",
        "speaking_order",
        "speaker_index",
        "lap",
        "ready_ids",
        "votes",
        "guesses",
        "discuss_ends_at",
        "winner",
        "win_reason",
        "guessed_by",
        "clue_unlocked",
        "eliminated_ids",
        "score_delta",
        "vote_counts",
        "prompt",
    }


def serialized_room_fields() -> set[str]:
    """Field names covered by `room_to_dict`, so new state cannot slip through."""
    return {
        "code",
        "players",
        "remaining_words",
        "used_words",
        "num_imposters",
        "discuss_seconds",
        "pass_and_play",
        "words_visible",
        "irl_mode",
        "imposter_hints",
        "phase",
        "round",
        "round_number",
        "created_at",
        "updated_at",
        "invites",
        "word_sources",
        "used_prompt_ids",
        "imposter_counts",
        "starter_counts",
        "last_imposter_ids",
        "last_speaking_order",
        "last_seen_at",
        "version",
    }


def _refresh_players(target: Room, source: Room) -> None:
    for player_id, incoming in source.players.items():
        seated = target.players.get(player_id)
        if seated is None:
            target.players[player_id] = incoming
            continue
        for spot in fields(Player):
            if spot.name == "token" and not getattr(incoming, "token"):
                continue  # stored rooms only keep the hash; keep a freshly issued token
            setattr(seated, spot.name, getattr(incoming, spot.name))
    for player_id in [pid for pid in target.players if pid not in source.players]:
        del target.players[player_id]


def _refresh_invites(target: Room, source: Room) -> None:
    for token, incoming in source.invites.items():
        current = target.invites.get(token)
        if current is None:
            target.invites[token] = incoming
            continue
        for spot in fields(Invite):
            setattr(current, spot.name, getattr(incoming, spot.name))
    for token in [key for key in target.invites if key not in source.invites]:
        del target.invites[token]


def _refresh_round(target: Room, source: Room) -> None:
    if source.round is None or target.round is None:
        target.round = source.round
        return
    for spot in fields(RoundState):
        setattr(target.round, spot.name, getattr(source.round, spot.name))


def refresh_room(target: Room, source: Room) -> None:
    """Pull stored state into the copy this process already handed out.

    Callers hold on to `Room`, `Player` and `RoundState` objects between calls,
    so a reload updates those objects in place rather than replacing them.
    """
    nested = {"players", "round", "invites"}
    for spot in fields(Room):
        if spot.name not in nested:
            setattr(target, spot.name, getattr(source, spot.name))
    _refresh_players(target, source)
    _refresh_invites(target, source)
    _refresh_round(target, source)


class RoomStore(Protocol):
    """Where tables live between requests."""

    def load(self, code: str) -> Room | None: ...

    def save(self, room: Room) -> None: ...

    def delete(self, code: str) -> None: ...

    def sweep(self, older_than: float) -> None: ...


class MemoryStore:
    """One process, one dict. LAN play and tests never leave this process."""

    kind = "memory"

    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def load(self, code: str) -> Room | None:
        return self.rooms.get(code)

    def save(self, room: Room) -> None:
        room.version += 1
        self.rooms[room.code] = room

    def delete(self, code: str) -> None:
        self.rooms.pop(code, None)

    def sweep(self, older_than: float) -> None:
        stale = [code for code, room in self.rooms.items() if room.last_seen_at < older_than]
        for code in stale:
            self.rooms.pop(code, None)


class GameHub:
    def __init__(
        self,
        rng: random.Random | None = None,
        store: RoomStore | None = None,
        idle_seconds: float = ROOM_IDLE_SECONDS,
    ) -> None:
        self.store: RoomStore = store if store is not None else MemoryStore()
        self.idle_seconds = idle_seconds
        self.rng = rng if rng is not None else random.SystemRandom()
        self._live: dict[str, Room] = {}

    def create_room(self, host_name: str) -> tuple[Room, Player]:
        name = _clean_name(host_name)
        code = self._unique_code()
        host = _seat(code, name, is_host=True)
        room = Room(code=code, players={host.id: host})
        self._live[code] = room
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
        player = _seat(
            room.code,
            cleaned,
            is_host=False,
            phone=invite.phone if invite else None,
        )
        room.players[player.id] = player
        if invite:
            invite.claimed_by = player.id
        self._touch(room)
        return room, player

    def _reclaim_player(self, room: Room, player: Player) -> tuple[Room, Player]:
        """Sit somebody back down in the seat they already had.

        Their old token stops working: the seat answers to the new one only.
        """
        player.token = _session_token(room.code, player.id)
        player.token_hash = hash_token(player.token)
        player.connected = True
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
        invite = Invite(token=_invite_token(room.code), name=cleaned_name, phone=cleaned_phone)
        room.invites[invite.token] = invite
        self._touch(room)
        return invite

    def lookup_invite(self, token: str) -> tuple[Room, Invite]:
        cleaned = (token or "").strip()
        code = _read_invite_token(cleaned)
        if not code:
            raise GameError("That invite is no longer valid.", 404, "invite_invalid")
        room = self._load(code)
        if room is None:
            raise GameError("That room has closed.", 404, "room_closed")
        invite = room.invites.get(cleaned)
        if invite is None:
            raise GameError("That invite is no longer valid.", 404, "invite_invalid")
        if invite.claimed_by and invite.claimed_by in room.players:
            raise GameError("That invite was already used.", 400, "invite_claimed")
        return room, invite

    def resolve_token(self, token: str | None, now: float | None = None) -> tuple[Room, Player]:
        """Re-attach a phone from its token alone, whichever process is serving."""
        if not token:
            raise GameError("Sign in to this room first.", 401, "no_session")
        parsed = _read_session_token(token)
        if parsed is None:
            raise GameError("Session expired. Join again with the same name.", 401, "session_invalid")
        code, player_id = parsed
        room = self._load(code)
        if room is None:
            raise GameError("That room has closed.", 404, "room_closed")
        player = room.players.get(player_id)
        if player is None or not secrets.compare_digest(player.token_hash, hash_token(token)):
            raise GameError("You are no longer in this room. Join again with the same name.", 401, "not_seated")
        self._mark_seen(room, player, now=now)
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
            if cleaned_mode not in PROMPT_MODES:
                raise GameError("Pick a prompt style from the list.")
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

    def next_prompt(self, room: Room, host: Player) -> dict[str, str] | None:
        """Host swaps the shared prompt during reveal, the circle, or open floor."""
        self._require_host(host)
        self.tick(room)
        if room.phase not in ("reveal", "discuss", "huddle") or room.round is None:
            raise GameError("Prompts can change during reveal or discussion.")
        if room.irl_mode == "off":
            raise GameError("Pick a prompt style to deal a prompt.")
        prompt = self._deal_prompt(room, room.round.word, extra_exclude={
            room.round.prompt["id"] if room.round.prompt else ""
        })
        room.round.prompt = prompt
        self._touch(room)
        return prompt

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
        """Drop tables nobody has opened for a long time.

        A phone that is still polling counts as activity, so a lobby waiting on
        late guests stays open however long the wait takes.
        """
        clock = time.time() if now is None else now
        cutoff = clock - self.idle_seconds
        self.store.sweep(cutoff)
        for code in [key for key, room in self._live.items() if room.last_seen_at < cutoff]:
            self._live.pop(code, None)

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
        room.imposter_counts.pop(player.id, None)
        room.starter_counts.pop(player.id, None)
        if player.is_host and room.players:
            successor = next(iter(room.players.values()))
            successor.is_host = True
        if not room.players:
            self._live.pop(room.code, None)
            self.store.delete(room.code)
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
            delayed_ok = room.pass_and_play or rnd.clue_unlocked
            show_clue = bool(rnd.clue) and (
                room.phase in ("results", "ended")
                or (room.imposter_hints and delayed_ok)
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
        room = self._load(key)
        if room is None:
            raise GameError("No room with that code.", 404, "room_closed")
        return room

    def _load(self, code: str) -> Room | None:
        """Read a table, reusing the copy this process already has of it."""
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
            code = "".join(self.rng.choice(CODE_ALPHABET) for _ in range(4))
            if self._load(code) is None:
                return code
        raise GameError("Could not create a room. Try again.", 500)

    def _touch(self, room: Room, at: float | None = None) -> None:
        room.updated_at = time.time() if at is None else at
        room.last_seen_at = max(room.last_seen_at, room.updated_at)
        self.store.save(room)

    def _mark_seen(self, room: Room, player: Player, now: float | None = None) -> None:
        """Reading the room keeps it alive, without a store write per poll."""
        clock = time.time() if now is None else now
        player.connected = True
        player.last_seen = clock
        if clock - room.last_seen_at < ACTIVITY_WRITE_SECONDS:
            return
        room.last_seen_at = clock
        try:
            self.store.save(room)
        except StoreConflict:
            # Somebody else just wrote this room, which refreshed it anyway.
            pass

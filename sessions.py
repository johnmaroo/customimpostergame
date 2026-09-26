"""Room codes, seats and session tokens, shared by the games on this site.

Every game here works the same way from the phone's side: you are handed a
bearer token once, and every later request has to find your seat again —
possibly in a different process, because a serverless host can answer two
taps from two instances.

So a token carries three things joined by dots: the room code, the seat id,
and a random secret. The first two are in the clear on purpose, because they
tell whichever instance picks up the request which single room to load. Only
the hash of the whole token is ever written down, so a stolen copy of the
room data does not let anyone sit in a seat.
"""

from __future__ import annotations

import hashlib
import random
import secrets

from errors import GameError

# No I, O, 0 or 1: these get read aloud across a room and typed on an iPad.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 4
MAX_NAME_LEN = 24
TOKEN_SEPARATOR = "."


def new_id() -> str:
    return secrets.token_urlsafe(9)


def hash_token(token: str) -> str:
    """Rooms are written to disk or a shared cache, so only hashes are stored."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def session_token(code: str, seat_id: str) -> str:
    """A token that says which room and seat it belongs to, plus a secret."""
    return TOKEN_SEPARATOR.join((code, seat_id, secrets.token_urlsafe(24)))


def read_session_token(token: str) -> tuple[str, str] | None:
    """Pull the room code and seat id back out, or None if this is not one of ours."""
    parts = (token or "").strip().split(TOKEN_SEPARATOR)
    if len(parts) != 3 or not all(parts):
        return None
    code, seat_id, _secret = parts
    return code.upper(), seat_id


def clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise GameError("Enter a name to join.")
    if len(cleaned) > MAX_NAME_LEN:
        raise GameError(f"Names can be at most {MAX_NAME_LEN} characters.")
    return cleaned


def random_code(rng: random.Random) -> str:
    return "".join(rng.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))

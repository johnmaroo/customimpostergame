"""The failures a game has to be able to explain to the person holding a phone.

These live on their own because more than one game raises them and because
the store backends raise two of them without knowing which game they are
keeping. ``engine`` re-exports all three, so importing them from there keeps
working.
"""

from __future__ import annotations


class GameError(Exception):
    """Something the player did that the game can explain back to them."""

    def __init__(self, message: str, status_code: int = 400, code: str = "game_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class StoreConflict(Exception):
    """Another writer changed this room first; the caller should retry."""


class StoreUnavailable(Exception):
    """The room store could not be reached. The table is not lost, just unreadable."""

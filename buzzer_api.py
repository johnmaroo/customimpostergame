"""The buzzer's pages and its API.

The interesting part of this file is how an iPad finds out that the buzzers
have opened, because that is the whole game. Polling every couple of seconds,
the way the party game does, would mean the winner was whoever's poll happened
to land nearest the host's tap — a two second lottery decided before anybody
moved a finger.

So a device asks for the room and says which version it already has. If
nothing has changed the request is held open instead of answered, and it
comes back the moment something does. Two things can end the hold:

* a write on this instance, which wakes every held request for that room
  straight away — this is the whole story for a laptop hosting over Wi-Fi;
* a check of the store, for a deployment running more than one instance,
  where the host's tap lands somewhere this process cannot see.

Those store checks happen on a shared clock grid rather than on each
request's own schedule, so every waiting device looks at the same instants
and they all find out together. Spreading them out would put the skew back,
just in smaller pieces.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from buzzer import (
    BUZZER_ROOMS,
    DEFAULT_CLUE_VALUE,
    DEFAULT_FALSE_START_MS,
    MAX_CLUE_VALUE,
    MAX_FALSE_START_MS,
    MAX_PASSWORD_LEN,
    MIN_PASSWORD_LEN,
    BuzzError,
    BuzzHub,
    BuzzRoom,
    Seat,
)
from notify import qr_svg
from sessions import MAX_NAME_LEN, read_session_token
from store import create_store, describe_store, room_ttl_seconds, version_of
from web import bearer_token, public_origin, retry_on_conflict

STATIC = Path(__file__).resolve().parent / "static"
PAGE = STATIC / "buzzer.html"
SWEEP_INTERVAL_SECONDS = 60.0


def _seconds(name: str, fallback: float, ceiling: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return min(max(value, 0.0), ceiling)


# How long one held request stays open. It has to sit under the host's own
# request timeout: a serverless function that is cut off mid-hold costs the
# device a round trip it could have spent waiting.
HOLD_SECONDS = _seconds("BUZZER_HOLD_SECONDS", 20.0, 55.0)
MAX_HOLD_SECONDS = 55.0
# How often a held request looks at the store for a change another instance
# made. Only a deployment running more than one instance pays for this; a
# laptop on the Wi-Fi is woken directly and never waits for a tick.
TICK_SECONDS = max(_seconds("BUZZER_TICK_SECONDS", 0.25, 5.0), 0.02)

hub = BuzzHub(store=create_store(ttl_seconds=room_ttl_seconds(), rooms=BUZZER_ROOMS))
room_store = describe_store(hub.store)
lock = threading.RLock()
last_sweep = 0.0

router = APIRouter()


class Wakeups:
    """Held requests, waiting to be told their room just changed.

    A write on this instance wakes every device watching that room with no
    store read at all, which is what makes the buzzers open at the same
    instant on a laptop hosting over Wi-Fi.

    Each waiter brings its own event and the loop that event belongs to, so
    waking one is safe from any thread and from any other loop — the wake
    simply does not reach a loop that has since gone away, and that waiter
    falls back to noticing the change in the store.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    def wake(self, code: str) -> None:
        with self._lock:
            waiting = self._waiters.pop(code, [])
        for loop, event in waiting:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)

    async def wait(self, code: str, timeout: float) -> None:
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.setdefault(code, []).append(waiter)
        try:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(waiter[1].wait(), timeout)
        finally:
            with self._lock:
                rest = self._waiters.get(code)
                if rest is not None:
                    if waiter in rest:
                        rest.remove(waiter)
                    if not rest:
                        self._waiters.pop(code, None)


wakeups = Wakeups()


class HostBody(BaseModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LEN)


class ClaimBody(BaseModel):
    code: str = Field(min_length=4, max_length=4)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LEN)


class JoinBody(BaseModel):
    code: str = Field(min_length=4, max_length=4)
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)


class JudgeBody(BaseModel):
    correct: bool


class SettingsBody(BaseModel):
    clueValue: int | None = Field(default=None, ge=0, le=MAX_CLUE_VALUE)
    falseStartMs: int | None = Field(default=None, ge=0, le=MAX_FALSE_START_MS)


class ScoreBody(BaseModel):
    seatId: str = Field(min_length=1, max_length=64)
    delta: int = Field(ge=-MAX_CLUE_VALUE, le=MAX_CLUE_VALUE)


class SeatBody(BaseModel):
    seatId: str = Field(min_length=1, max_length=64)


def _snapshot(room: BuzzRoom, seat: Seat, request: Request) -> dict[str, Any]:
    view = hub.view_for(room, seat)
    join_url = f"{public_origin(request)}/buzzer/{room.code}"
    view["joinUrl"] = join_url
    if seat.is_host:
        view["joinQrSvg"] = qr_svg(join_url)
    return view


def _read(token: str | None, request: Request) -> dict[str, Any]:
    with lock:
        room, seat = hub.resolve_token(token)
        return _snapshot(room, seat, request)


def _change(
    token: str | None,
    request: Request,
    action: Callable[[BuzzRoom, Seat], Any] | None = None,
    respond: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> Any:
    """Re-attach the device, apply one change, and answer with a snapshot."""

    def once() -> tuple[Any, dict[str, Any]]:
        with lock:
            room, seat = hub.resolve_token(token)
            outcome = action(room, seat) if action else None
            return outcome, _snapshot(room, seat, request)

    outcome, view = retry_on_conflict(once)
    return respond(outcome, view) if respond else view


async def _apply(
    authorization: str | None,
    request: Request,
    action: Callable[[BuzzRoom, Seat], Any] | None = None,
    respond: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> Any:
    """Run one change off the event loop, then wake the room's held requests."""
    token = bearer_token(authorization)
    answer = await asyncio.to_thread(_change, token, request, action, respond)
    code = _code_in(answer) or _room_code(token)
    if code:
        wakeups.wake(code)
    return answer


def _code_in(answer: Any) -> str:
    if not isinstance(answer, dict):
        return ""
    if isinstance(answer.get("room"), dict):
        return str(answer["room"].get("code") or "")
    return str(answer.get("code") or "")


def _room_code(token: str | None) -> str:
    parsed = read_session_token(token or "")
    return parsed[0] if parsed else ""


def _sweep_idle_rooms() -> None:
    """Retire abandoned rooms now and then, never on the critical path."""
    global last_sweep
    now = time.time()
    with lock:
        if now - last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        last_sweep = now
    hub.sweep_idle(now)


# -- pages ---------------------------------------------------------------


@router.get("/buzzer")
def buzzer_page() -> FileResponse:
    return FileResponse(PAGE)


@router.get("/buzzer/{code}")
def buzzer_join_page(code: str) -> FileResponse:
    return FileResponse(PAGE)


# -- getting in ----------------------------------------------------------


@router.get("/api/buzz/meta")
def meta() -> dict[str, Any]:
    return {
        "minPasswordLength": MIN_PASSWORD_LEN,
        "defaultClueValue": DEFAULT_CLUE_VALUE,
        "defaultFalseStartMs": DEFAULT_FALSE_START_MS,
        "holdSeconds": HOLD_SECONDS,
        "roomStore": {
            "kind": room_store.kind,
            "shared": room_store.shared,
            "detail": room_store.detail,
        },
    }


@router.post("/api/buzz/rooms")
async def open_room(body: HostBody, request: Request) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        with lock:
            room, host = hub.create_room(body.password)
            return {"token": host.token, "seatId": host.id, "room": _snapshot(room, host, request)}

    return await asyncio.to_thread(retry_on_conflict, once)


@router.post("/api/buzz/rooms/host")
async def take_console(body: ClaimBody, request: Request) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        with lock:
            room, host = hub.claim_host(body.code, body.password)
            return {"token": host.token, "seatId": host.id, "room": _snapshot(room, host, request)}

    answer = await asyncio.to_thread(retry_on_conflict, once)
    wakeups.wake(answer["room"]["code"])
    return answer


@router.post("/api/buzz/rooms/join")
async def join_room(body: JoinBody, request: Request) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        with lock:
            room, seat = hub.join_room(body.code, body.name)
            return {"token": seat.token, "seatId": seat.id, "room": _snapshot(room, seat, request)}

    answer = await asyncio.to_thread(retry_on_conflict, once)
    wakeups.wake(answer["room"]["code"])
    return answer


# -- watching the room ---------------------------------------------------


@router.get("/api/buzz/room")
async def read_room(
    request: Request,
    since: str = Query(default=""),
    wait: float = Query(default=0.0, ge=0.0),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """The room as this device should see it, held open until it changes.

    Without `since` this answers straight away. With the revision the device
    already has, and a `wait`, the answer is held back until the room moves
    on from that revision or the hold runs out.
    """
    _sweep_idle_rooms()
    token = bearer_token(authorization)
    view = await asyncio.to_thread(_read, token, request)
    hold = min(wait, MAX_HOLD_SECONDS)
    if not since or hold <= 0 or view["rev"] != since:
        return view
    return await _hold(token, request, view, since, hold)


async def _hold(
    token: str | None,
    request: Request,
    view: dict[str, Any],
    since: str,
    hold: float,
) -> dict[str, Any]:
    code = str(view["code"])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + hold
    seen = await asyncio.to_thread(version_of, hub.store, code)
    while True:
        left = deadline - loop.time()
        if left <= 0:
            # Some of the room is the clock rather than the room: how long the
            # buzzers have been open, how much of a lockout is left. A reading
            # taken before the hold is already wrong by the length of it.
            return await asyncio.to_thread(_read, token, request)
        await wakeups.wait(code, min(_until_next_tick(), left))
        version = await asyncio.to_thread(version_of, hub.store, code)
        if version == seen:
            continue
        seen = version
        view = await asyncio.to_thread(_read, token, request)
        if view["rev"] != since:
            return view


def _until_next_tick(now: float | None = None) -> float:
    """Seconds until the next slot on a clock grid every instance shares.

    Waiting devices land on the same instants instead of each drifting on
    its own schedule, so when a change has to be found in the store rather
    than signalled locally, they all find it in the same pass.
    """
    clock = time.time() if now is None else now
    return (math.floor(clock / TICK_SECONDS) + 1) * TICK_SECONDS - clock


# -- the button ----------------------------------------------------------


@router.post("/api/buzz/room/open")
async def open_buzzers(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return await _apply(authorization, request, lambda room, seat: hub.open_buzzers(room, seat))


@router.post("/api/buzz/room/buzz")
async def buzz(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """One tap. A tap that did not win is an answer, not an error.

    The device gets the verdict and the room it produced in the same reply,
    so it can say who beat them without asking a second time.
    """
    token = bearer_token(authorization)

    def once() -> dict[str, Any]:
        with lock:
            room, seat = hub.resolve_token(token)
            try:
                hub.buzz(room, seat)
            except BuzzError as exc:
                verdict = {"tookIt": False, "reason": exc.code, "message": exc.message}
            else:
                verdict = {"tookIt": True, "reason": "", "message": ""}
            return {**verdict, "room": _snapshot(room, seat, request)}

    answer = await asyncio.to_thread(retry_on_conflict, once)
    if answer["tookIt"]:
        wakeups.wake(answer["room"]["code"])
    return answer


@router.post("/api/buzz/room/judge")
async def judge(
    body: JudgeBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return await _apply(
        authorization, request, lambda room, seat: hub.judge(room, seat, body.correct)
    )


@router.post("/api/buzz/room/next")
async def next_clue(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return await _apply(authorization, request, lambda room, seat: hub.next_clue(room, seat))


# -- the scoreboard ------------------------------------------------------


@router.post("/api/buzz/room/settings")
async def settings(
    body: SettingsBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return await _apply(
        authorization,
        request,
        lambda room, seat: hub.set_settings(
            room, seat, clue_value=body.clueValue, false_start_ms=body.falseStartMs
        ),
    )


@router.post("/api/buzz/room/score")
async def score(
    body: ScoreBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return await _apply(
        authorization,
        request,
        lambda room, seat: hub.adjust_score(room, seat, body.seatId, body.delta),
    )


@router.post("/api/buzz/room/scores/reset")
async def reset_scores(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return await _apply(authorization, request, lambda room, seat: hub.reset_scores(room, seat))


@router.post("/api/buzz/room/kick")
async def kick(
    body: SeatBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return await _apply(
        authorization, request, lambda room, seat: hub.kick(room, seat, body.seatId)
    )


@router.post("/api/buzz/room/leave")
async def leave(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = bearer_token(authorization)

    def once() -> dict[str, Any]:
        with lock:
            room, seat = hub.resolve_token(token)
            hub.leave(room, seat)
            return {"ok": True}

    answer = await asyncio.to_thread(retry_on_conflict, once)
    code = _room_code(token)
    if code:
        wakeups.wake(code)
    return answer

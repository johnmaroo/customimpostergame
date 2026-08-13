"""Local web server for the Imposter party game.

Laptop and phones on the same Wi-Fi still use:

    python game.py

The existing `static/` UI is also attached with FastAPI's frontend helper so
the same app can be connected to a Vercel project without replacing LAN play.
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clues import get_or_create_clue, load_env_file, resolve_api_key
from engine import GameError, GameHub, Player, Room, StoreConflict, StoreUnavailable
from notify import (
    deliver_invite,
    imessage_available,
    invite_message,
    mask_phone,
    normalize_phone,
    qr_svg,
    sms_url,
)
from packs import get_pack, list_packs
from store import create_store, room_ttl_seconds
from wordbank import WordBank

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
# Two instances can both write a room; the loser of the race replays its request.
WRITE_ATTEMPTS = 4
SWEEP_INTERVAL_SECONDS = 60.0

load_env_file()

room_ttl = room_ttl_seconds()
hub = GameHub(store=create_store(ttl_seconds=room_ttl), idle_seconds=room_ttl)
bank = WordBank()
lock = threading.RLock()
listen_port = 8765
last_sweep = 0.0

app = FastAPI(title="Imposter", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return []
    return [part.strip().rstrip("/") for part in raw.split(",") if part.strip()]


_cors = _cors_origins()
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=24)


class JoinBody(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    code: str = Field(min_length=4, max_length=4)
    inviteToken: str | None = None


class WordBody(BaseModel):
    word: str = Field(min_length=1, max_length=48)


class PackBody(BaseModel):
    packId: str


class SettingsBody(BaseModel):
    numImposters: int | None = None
    discussSeconds: int | None = None
    passAndPlay: bool | None = None
    wordsVisible: bool | None = None
    irlMode: str | None = None
    imposterHints: bool | None = None


class GuessBody(BaseModel):
    word: str | None = None


class VoteBody(BaseModel):
    targetId: str | None = None


class PeekBody(BaseModel):
    playerId: str


class KickBody(BaseModel):
    playerId: str


class InviteBody(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    phone: str = Field(min_length=7, max_length=24)


def _token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


def _error(exc: GameError) -> JSONResponse:
    return JSONResponse(
        {"error": exc.message, "code": exc.code},
        status_code=exc.status_code,
    )


def _retry(work: Callable[[], Any]) -> Any:
    """Replay a request when another instance wrote the same room first."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            return work()
        except StoreConflict:
            if attempt == WRITE_ATTEMPTS - 1:
                raise GameError(
                    "The table changed while you tapped. Try that again.",
                    409,
                    "write_conflict",
                ) from None
    raise GameError("The table changed while you tapped. Try that again.", 409, "write_conflict")


def _play(
    authorization: str | None,
    request: Request,
    action: Callable[[Room, Player], Any] | None = None,
    respond: Callable[[Any, dict[str, Any]], Any] | None = None,
) -> Any:
    """Re-attach the caller's session, apply one change, and answer with a snapshot."""
    token = _token(authorization)

    def once() -> tuple[Any, dict[str, Any]]:
        with lock:
            room, player = hub.resolve_token(token)
            outcome = action(room, player) if action else None
            return outcome, _snapshot(room, player, request)

    outcome, view = _retry(once)
    return respond(outcome, view) if respond else view


def _sweep_idle_rooms() -> None:
    """Retire abandoned tables now and then, never on the critical path."""
    global last_sweep
    now = time.time()
    with lock:
        if now - last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        last_sweep = now
    try:
        hub.sweep_idle(now)
    except StoreUnavailable:
        pass


def _persist_word(word: str, fallback_clue: str | None = None) -> None:
    bank.add_word(word)
    if fallback_clue:
        bank.set_clue_if_empty(word, fallback_clue)

    def upgrade() -> None:
        try:
            from clues import generate_category_clue

            clue = generate_category_clue(word)
            with lock:
                bank.set_clue(word, clue)
        except Exception:
            if fallback_clue:
                with lock:
                    bank.set_clue_if_empty(word, fallback_clue)

    threading.Thread(target=upgrade, daemon=True).start()


def _clue_for(word: str) -> str | None:
    return get_or_create_clue(bank, word)


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def public_origin(request: Request) -> str:
    """Origin phones should open. Localhost is rewritten to the LAN address."""
    configured = (
        os.getenv("PUBLIC_ORIGIN") or os.getenv("IMPOSTER_PUBLIC_ORIGIN") or ""
    ).strip().rstrip("/")
    if configured:
        return configured
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host_header = forwarded_host or request.headers.get("host") or f"127.0.0.1:{listen_port}"
    hostname, separator, port = host_header.partition(":")
    proto = (
        (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
        .split(",")[0]
        .strip()
    )
    if hostname in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        hostname = lan_ip()
        port = port or str(listen_port)
        return f"http://{hostname}:{port}"
    if not separator:
        return f"{proto}://{host_header}"
    return f"{proto}://{host_header}"


def _snapshot(room, player, request: Request) -> dict[str, Any]:
    view = hub.view_for(room, player)
    origin = public_origin(request)
    join_url = f"{origin}/join/{room.code}"
    view["joinUrl"] = join_url
    view["joinQrSvg"] = qr_svg(join_url)
    view["canIMessage"] = imessage_available()
    for invite in view.get("invites") or []:
        invited_url = f"{join_url}?invite={invite['token']}"
        message = invite_message(room.code, invited_url, invite["name"])
        invite["phoneMasked"] = mask_phone(invite["phone"])
        invite["smsUrl"] = sms_url(invite["phone"], message)
        invite["joinUrl"] = invited_url
    return view


@app.exception_handler(GameError)
async def game_error_handler(_request: Request, exc: GameError) -> JSONResponse:
    return _error(exc)


@app.exception_handler(StoreUnavailable)
async def store_unavailable_handler(_request: Request, _exc: StoreUnavailable) -> JSONResponse:
    # The table still exists; this phone just could not reach it. Say so with a
    # retryable status so the app reconnects instead of dropping the session.
    return JSONResponse(
        {
            "error": "Cannot reach the table right now. Hold on.",
            "code": "store_unavailable",
        },
        status_code=503,
        headers={"Retry-After": "2"},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"error": "Check the name, room code, or word and try again.", "code": "invalid_input"},
        status_code=400,
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/join/{code}")
def join_page(code: str) -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta(request: Request) -> dict[str, Any]:
    origin = public_origin(request)
    return {
        "hasAiKey": bool(resolve_api_key()),
        "savedWordCount": len(bank.all_words()),
        "packs": list_packs(),
        "joinOrigin": origin,
        "canIMessage": imessage_available(),
    }


@app.post("/api/rooms")
def create_room(body: NameBody, request: Request) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        with lock:
            room, player = hub.create_room(body.name)
            saved = bank.unused_words()
            return {
                "token": player.token,
                "playerId": player.id,
                "savedWordCount": len(saved),
                "room": _snapshot(room, player, request),
            }

    return _retry(once)


@app.post("/api/rooms/join")
def join_room(body: JoinBody, request: Request) -> dict[str, Any]:
    def once() -> dict[str, Any]:
        with lock:
            room, player = hub.join_room(body.code, body.name, invite_token=body.inviteToken)
            return {
                "token": player.token,
                "playerId": player.id,
                "room": _snapshot(room, player, request),
            }

    return _retry(once)


@app.get("/api/invites/{token}")
def read_invite(token: str) -> dict[str, Any]:
    with lock:
        room, invite = hub.lookup_invite(token)
    return {
        "code": room.code,
        "name": invite.name,
        "phoneMasked": mask_phone(invite.phone),
    }


@app.get("/api/room")
def get_room(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _sweep_idle_rooms()
    return _play(authorization, request)


@app.post("/api/room/settings")
def settings(
    body: SettingsBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _play(
        authorization,
        request,
        lambda room, player: hub.set_settings(
            room,
            player,
            num_imposters=body.numImposters,
            discuss_seconds=body.discussSeconds,
            pass_and_play=body.passAndPlay,
            words_visible=body.wordsVisible,
            irl_mode=body.irlMode,
            imposter_hints=body.imposterHints,
        ),
    )


@app.post("/api/room/words")
def add_word(
    body: WordBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    def action(room: Room, player: Player) -> None:
        _persist_word(hub.add_word(room, player, body.word))

    return _play(authorization, request, action)


@app.post("/api/room/words/saved")
def load_saved(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    def action(room: Room, player: Player) -> None:
        words = bank.unused_words()
        if not words:
            raise GameError("No saved words yet. Add some here and they will stick around.")
        hub.add_words(room, player, words)

    return _play(authorization, request, action)


@app.post("/api/room/words/pack")
def add_pack(
    body: PackBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    pack = get_pack(body.packId)

    def action(room: Room, player: Player) -> None:
        for word, fallback in pack["words"]:
            added = hub.add_word(room, player, word, source=pack["id"])
            _persist_word(added, fallback_clue=fallback)

    return _play(authorization, request, action)


@app.post("/api/room/words/recycle")
def recycle(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.recycle_words(room, player))


@app.post("/api/room/words/remove")
def remove_word(
    body: WordBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _play(
        authorization,
        request,
        lambda room, player: hub.remove_word(room, player, body.word),
    )


@app.post("/api/room/invite")
def invite_player(
    body: InviteBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    phone = normalize_phone(body.phone)

    def respond(invite: Any, view: dict[str, Any]) -> dict[str, Any]:
        invited_url = f"{view['joinUrl']}?invite={invite.token}"
        message = invite_message(view["code"], invited_url, invite.name)
        delivery = deliver_invite(invite.phone, message)
        return {
            "room": view,
            "sent": delivery["sent"],
            "method": delivery["method"],
            "smsUrl": delivery["smsUrl"],
            "inviteToken": invite.token,
        }

    return _play(
        authorization,
        request,
        lambda room, player: hub.add_invite(room, player, body.name, phone),
        respond,
    )


@app.post("/api/room/start")
def start_round(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    def action(room: Room, player: Player) -> None:
        rnd = hub.start_round(room, player, clue=None)
        rnd.clue = _clue_for(rnd.word) if room.imposter_hints else None
        bank.mark_used(rnd.word)

    return _play(authorization, request, action)


@app.post("/api/room/ready")
def ready(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.mark_ready(room, player))


@app.post("/api/room/peek")
def peek(
    body: PeekBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _play(
        authorization,
        request,
        lambda room, player: hub.peek_role(room, player, body.playerId),
        lambda role, view: {"role": role, "room": view},
    )


@app.post("/api/room/next-speaker")
def next_speaker(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.next_speaker(room, player))


@app.post("/api/room/around-again")
def around_again(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.go_around_again(room, player))


@app.post("/api/room/next-prompt")
def next_prompt(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.next_prompt(room, player))


@app.post("/api/room/guess")
def guess_word(
    body: GuessBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _play(
        authorization,
        request,
        lambda room, player: hub.guess_word(room, player, body.word),
    )


@app.post("/api/room/advance")
def advance(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.advance(room, player))


@app.post("/api/room/vote")
def vote(
    body: VoteBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _play(
        authorization,
        request,
        lambda room, player: hub.vote(room, player, body.targetId),
    )


@app.post("/api/room/kick")
def kick(
    body: KickBody,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    return _play(
        authorization,
        request,
        lambda room, player: hub.kick(room, player, body.playerId),
    )


@app.post("/api/room/end")
def end_game(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.end_game(room, player))


@app.post("/api/room/reopen")
def reopen_lobby(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    return _play(authorization, request, lambda room, player: hub.reopen_lobby(room, player))


@app.post("/api/room/leave")
def leave(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = _token(authorization)

    def once() -> dict[str, Any]:
        with lock:
            room, player = hub.resolve_token(token)
            hub.leave(room, player)
            return {"ok": True}

    return _retry(once)


# Existing phone UI in static/. API routes stay first; Vercel promotes these
# files to the CDN. python game.py still hosts laptop + phones on Wi-Fi.
app.frontend("/", directory=str(STATIC), fallback="index.html", check_dir=False)


def main() -> None:
    global listen_port
    parser = argparse.ArgumentParser(description="Imposter party game")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    listen_port = args.port
    display_ip = lan_ip() if args.host in {"0.0.0.0", "::"} else args.host
    print("\n  IMPOSTER")
    print("  --------")
    print(f"  This computer:  http://127.0.0.1:{args.port}")
    print(f"  Phones (Wi-Fi): http://{display_ip}:{args.port}")
    print("  Friends can scan the QR in the lobby or get a texted join link.")
    print("  Vercel is optional extra hosting — LAN play does not need it.\n")
    if not resolve_api_key():
        print("  Tip: add AI_GATEWAY_API_KEY to .env for custom-word category clues.")
        print("  Starter packs still include fallback categories.\n")
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()


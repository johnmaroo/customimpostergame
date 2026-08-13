"""Join links, QR codes, and text invites for Imposter."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from urllib.parse import quote

from engine import GameError

try:
    import segno
except ImportError:  # pragma: no cover
    segno = None


def normalize_phone(raw: str) -> str:
    """Accept common US and +country formats; store as +digits."""
    text = (raw or "").strip()
    if not text:
        raise GameError("Enter a phone number.")
    compact = re.sub(r"[^\d+]", "", text)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    digits = re.sub(r"\D", "", compact)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) < 11 or len(digits) > 15:
        raise GameError("Enter a full phone number, like 5551234567 or +44…")
    return "+" + digits


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "••••"
    return f"•••{digits[-4:]}"


def invite_message(code: str, join_url: str, invitee_name: str | None = None) -> str:
    who = f"{invitee_name}, join" if invitee_name else "Join"
    return f"{who} Imposter room {code}: {join_url}"


def sms_url(phone: str, message: str) -> str:
    number = normalize_phone(phone) if not str(phone).startswith("+") else phone
    return f"sms:{number}?body={quote(message)}"


_QR_CACHE: dict[str, str] = {}


def qr_svg(data: str) -> str:
    cached = _QR_CACHE.get(data)
    if cached is not None:
        return cached
    if segno is None:
        raise RuntimeError("segno is required to draw QR codes")
    qr = segno.make(data, error="m")
    svg = qr.svg_inline(scale=8, border=2, dark="#100c0a", light="#f6f0e6")
    _QR_CACHE[data] = svg
    return svg


def imessage_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _escape_for_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_imessage(recipient: str, message: str, service_hint: str | None = None) -> bool:
    """Send an iMessage on macOS. Returns False when Messages is unavailable."""
    if not imessage_available():
        return False
    msg = _escape_for_applescript(message)
    rcpt = _escape_for_applescript(recipient)
    if service_hint:
        service_clause = f"of {service_hint}"
    else:
        service_clause = 'of (service 1 whose service type is iMessage)'
    script = (
        f'tell application "Messages" to send "{msg}" '
        f'to buddy "{rcpt}" {service_clause}'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def deliver_invite(
    phone: str,
    message: str,
    *,
    send=None,
) -> dict[str, str | bool]:
    send_fn = send or send_imessage
    sent = False
    try:
        sent = bool(send_fn(phone, message))
    except Exception:
        sent = False
    return {
        "sent": sent,
        "smsUrl": sms_url(phone, message),
        "method": "imessage" if sent else "sms",
    }

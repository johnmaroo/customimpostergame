"""Generate Imposter category clues through the Vercel AI Gateway."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
DEFAULT_MODEL = "openai/gpt-5.4-mini"

CompleteFn = Callable[..., str]


def load_env_file(path: str | Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file without overriding existing env vars."""
    env_path = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ.setdefault(key, value)


def resolve_api_key() -> str | None:
    return os.getenv("AI_GATEWAY_API_KEY") or os.getenv("VERCEL_OIDC_TOKEN")


def parse_category(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty category clue")
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{"):
        data = json.loads(text)
        text = str(data.get("category") or data.get("clue") or "").strip()
    text = text.strip(" \"'")
    if not text:
        raise ValueError("empty category clue")
    return text


def clue_contains_word(clue: str, word: str) -> bool:
    return word.casefold() in clue.casefold()


def generate_category_clue(
    word: str,
    *,
    complete: CompleteFn | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    cleaned = " ".join(word.strip().split())
    if not cleaned:
        raise ValueError("word is required")

    complete_fn = complete or gateway_complete
    model_id = model or os.getenv("IMPOSTER_CLUE_MODEL") or DEFAULT_MODEL
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You write category clues for the party game Imposter. "
                "Reply with a short category the secret word belongs to, "
                "so an imposter can talk around the topic without knowing the word. "
                "Do not include the secret word itself."
            ),
        },
        {"role": "user", "content": f"Secret word: {cleaned}"},
    ]
    raw = complete_fn(messages=messages, model=model_id, api_key=api_key)
    clue = parse_category(raw)
    if clue_contains_word(clue, cleaned):
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": "That clue includes the secret word. Give a broader category that does not.",
            }
        )
        raw = complete_fn(messages=messages, model=model_id, api_key=api_key)
        clue = parse_category(raw)
        if clue_contains_word(clue, cleaned):
            raise ValueError("category clue included the secret word")
    return clue


def gateway_complete(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str | None = None,
) -> str:
    key = api_key or resolve_api_key()
    if not key:
        raise RuntimeError(
            "No AI Gateway credentials found. Set AI_GATEWAY_API_KEY "
            "from https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway%2Fapi-keys"
        )
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=GATEWAY_BASE_URL)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "category_clue",
                "schema": {
                    "type": "object",
                    "properties": {"category": {"type": "string"}},
                    "required": ["category"],
                    "additionalProperties": False,
                },
            },
        },
    )
    return response.choices[0].message.content or ""


def get_or_create_clue(
    bank: Any,
    word: str,
    *,
    generate: Callable[[str], str] | None = None,
) -> str | None:
    existing = bank.get_clue(word)
    if existing:
        return existing
    generate_fn = generate or generate_category_clue
    try:
        clue = generate_fn(word)
    except Exception:
        return None
    bank.set_clue(word, clue)
    return clue

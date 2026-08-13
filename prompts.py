"""Shared IRL questions and actions for Imposter discussion turns.

Everyone at the table sees the same prompt, so answers can be compared.
Prompts stay vague on purpose: opinion and vibe, not facts about the secret.
That way an imposter with only a broad category can still take a turn.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Kind = Literal["ask", "do"]
IrlMode = Literal["off", "mix", "ask", "do"]

IRL_MODES: tuple[IrlMode, ...] = ("off", "mix", "ask", "do")


class Prompt(TypedDict):
    id: str
    kind: Kind
    text: str
    packs: tuple[str, ...] | None


PROMPTS: tuple[Prompt, ...] = (
    {
        "id": "hot-take",
        "kind": "ask",
        "text": "Hot take, one sentence. Don't name it.",
        "packs": None,
    },
    {
        "id": "recommend",
        "kind": "ask",
        "text": "Would you recommend this kind of thing?",
        "packs": None,
    },
    {
        "id": "who-into",
        "kind": "ask",
        "text": "Who here is most into this? Point.",
        "packs": None,
    },
    {
        "id": "overrated",
        "kind": "ask",
        "text": "Overrated, underrated, or about right?",
        "packs": None,
    },
    {
        "id": "one-word",
        "kind": "ask",
        "text": "One-word reaction. Not its name.",
        "packs": None,
    },
    {
        "id": "you-thing",
        "kind": "ask",
        "text": "Is this a you thing, or not really?",
        "packs": None,
    },
    {
        "id": "vague-story",
        "kind": "ask",
        "text": "A short story about it. Keep it vague.",
        "packs": None,
    },
    {
        "id": "vibe-check",
        "kind": "ask",
        "text": "What's the vibe — chill, extra, or meh?",
        "packs": None,
    },
    {
        "id": "more-of-this",
        "kind": "ask",
        "text": "Would you want more of this in your life?",
        "packs": None,
    },
    {
        "id": "table-into",
        "kind": "ask",
        "text": "Does this table seem into it?",
        "packs": None,
    },
    {
        "id": "face",
        "kind": "do",
        "text": "React with only your face.",
        "packs": None,
    },
    {
        "id": "thumbs",
        "kind": "do",
        "text": "Thumbs up, sideways, or down.",
        "packs": None,
    },
    {
        "id": "point-who",
        "kind": "do",
        "text": "Point at who would be into this.",
        "packs": None,
    },
    {
        "id": "vibe-hands",
        "kind": "do",
        "text": "Show the vibe with your hands. Not the thing itself.",
        "packs": None,
    },
    {
        "id": "mime-feeling",
        "kind": "do",
        "text": "Mime how it feels, not what it is.",
        "packs": None,
    },
    {
        "id": "freeze",
        "kind": "do",
        "text": "Freeze in a reaction to it.",
        "packs": None,
    },
    {
        "id": "energy",
        "kind": "do",
        "text": "Big energy or small energy — just a gesture.",
        "packs": None,
    },
    {
        "id": "pass-vibe",
        "kind": "do",
        "text": "Send the vibe to the person on your right. No naming.",
        "packs": None,
    },
)


def prompt_view(prompt: Prompt | None) -> dict[str, str] | None:
    if prompt is None:
        return None
    return {"id": prompt["id"], "kind": prompt["kind"], "text": prompt["text"]}


def eligible_prompts(mode: str, pack_id: str | None) -> list[Prompt]:
    if mode not in ("mix", "ask", "do"):
        return []
    kinds = {"ask", "do"} if mode == "mix" else {mode}
    pool: list[Prompt] = []
    for prompt in PROMPTS:
        if prompt["kind"] not in kinds:
            continue
        packs = prompt["packs"]
        if packs and pack_id not in packs:
            continue
        pool.append(prompt)
    return pool


def pick_prompt(
    rng,
    *,
    mode: str,
    pack_id: str | None = None,
    exclude: list[str] | set[str] | None = None,
) -> Prompt | None:
    """Pick one shared prompt. Prompts stay generic so a category still works."""
    if mode == "off":
        return None
    pool = eligible_prompts(mode, pack_id)
    if not pool:
        return None
    blocked = set(exclude or ())
    unused = [prompt for prompt in pool if prompt["id"] not in blocked]
    return rng.choice(unused or pool)

"""Shared prompts for Imposter discussion turns.

Everyone at the table sees the same prompt, so answers can be compared.
Prompts stay vague on purpose: opinion and vibe, not facts about the secret.
That way an imposter with only a broad category can still take a turn.

Prompts come in two styles. ``irl`` ones ask the table to react in the room —
a face, a gesture, a hot take. ``classic`` ones are the plain questions of the
original game: describe the thing without naming it. A table that wants one
and not the other picks a mode below.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Kind = Literal["ask", "do"]
Style = Literal["classic", "irl"]
PromptMode = Literal["off", "classic", "mix", "ask", "do"]

PROMPT_MODES: tuple[PromptMode, ...] = ("off", "classic", "mix", "ask", "do")
# Which prompts each mode draws from: the style it wants, and the kinds within it.
MODE_POOLS: dict[str, tuple[Style, frozenset[Kind]]] = {
    "classic": ("classic", frozenset({"ask"})),
    "mix": ("irl", frozenset({"ask", "do"})),
    "ask": ("irl", frozenset({"ask"})),
    "do": ("irl", frozenset({"do"})),
}


class Prompt(TypedDict):
    id: str
    kind: Kind
    style: Style
    text: str
    packs: tuple[str, ...] | None


def _deck(style: Style, rows: tuple[tuple[str, Kind, str], ...]) -> tuple[Prompt, ...]:
    """Stamp a style onto one deck so a prompt cannot be filed under the wrong one."""
    return tuple(
        {"id": pid, "kind": kind, "style": style, "text": text, "packs": None}
        for pid, kind, text in rows
    )


# The plain questions of the original game: say something true about the thing
# without naming it. An imposter holding only a category can still bluff these.
CLASSIC_PROMPTS: tuple[Prompt, ...] = _deck(
    "classic",
    (
        ("classic-describe", "ask", "Describe it in one sentence without naming it."),
        ("classic-how-often", "ask", "How often does this come up in your life?"),
        ("classic-where", "ask", "Where would you expect to run into one?"),
        ("classic-miss-it", "ask", "Would you miss it if it disappeared tomorrow?"),
        ("classic-worth-it", "ask", "Is it worth what it costs?"),
        ("classic-kid-knows", "ask", "Would a five-year-old know what this is?"),
        ("classic-alone", "ask", "On your own, or with other people?"),
        ("classic-time-of-day", "ask", "Morning, afternoon, or the middle of the night?"),
        ("classic-how-long", "ask", "How long does it usually last?"),
        ("classic-loud", "ask", "Loud or quiet?"),
        ("classic-practice", "ask", "Does it take any practice to get right?"),
        ("classic-last-time", "ask", "When was the last time, roughly?"),
        ("classic-ten-years", "ask", "More common now than ten years ago, or less?"),
        ("classic-first-word", "ask", "First word that comes to mind. Not its name."),
    ),
)

# Reactions in the room: opinion, face, gesture. Nothing factual to leak.
IRL_PROMPTS: tuple[Prompt, ...] = _deck(
    "irl",
    (
        ("hot-take", "ask", "Hot take, one sentence. Don't name it."),
        ("recommend", "ask", "Would you recommend this kind of thing?"),
        ("who-into", "ask", "Who here is most into this? Point."),
        ("overrated", "ask", "Overrated, underrated, or about right?"),
        ("one-word", "ask", "One-word reaction. Not its name."),
        ("you-thing", "ask", "Is this a you thing, or not really?"),
        ("vague-story", "ask", "A short story about it. Keep it vague."),
        ("vibe-check", "ask", "What's the vibe — chill, extra, or meh?"),
        ("more-of-this", "ask", "Would you want more of this in your life?"),
        ("table-into", "ask", "Does this table seem into it?"),
        ("face", "do", "React with only your face."),
        ("thumbs", "do", "Thumbs up, sideways, or down."),
        ("point-who", "do", "Point at who would be into this."),
        ("vibe-hands", "do", "Show the vibe with your hands. Not the thing itself."),
        ("mime-feeling", "do", "Mime how it feels, not what it is."),
        ("freeze", "do", "Freeze in a reaction to it."),
        ("energy", "do", "Big energy or small energy — just a gesture."),
        ("pass-vibe", "do", "Send the vibe to the person on your right. No naming."),
    ),
)

PROMPTS: tuple[Prompt, ...] = CLASSIC_PROMPTS + IRL_PROMPTS


def prompt_view(prompt: Prompt | None) -> dict[str, str] | None:
    if prompt is None:
        return None
    return {"id": prompt["id"], "kind": prompt["kind"], "text": prompt["text"]}


def eligible_prompts(mode: str, pack_id: str | None) -> list[Prompt]:
    pool_for = MODE_POOLS.get(mode)
    if pool_for is None:
        return []
    style, kinds = pool_for
    pool: list[Prompt] = []
    for prompt in PROMPTS:
        if prompt["style"] != style or prompt["kind"] not in kinds:
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

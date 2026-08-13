"""Shared IRL questions and actions for Imposter discussion turns.

Everyone at the table sees the same prompt, so answers can be compared.
Prompts are written to work if you know the secret word or only a category:
they never name a specific word, and they stay seated-table safe.
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
    # Questions — one sentence, comparable around the table.
    {
        "id": "who-likes",
        "kind": "ask",
        "text": "Who at this table would like it most? Point at them, then say why in one sentence.",
        "packs": None,
    },
    {
        "id": "own-borrow-avoid",
        "kind": "ask",
        "text": "Would you rather own it, borrow it, or never deal with it?",
        "packs": None,
    },
    {
        "id": "useful-fun",
        "kind": "ask",
        "text": "More useful, or more fun?",
        "packs": None,
    },
    {
        "id": "how-often",
        "kind": "ask",
        "text": "Daily, sometimes, or almost never — how often do you run into it?",
        "packs": None,
    },
    {
        "id": "road-trip",
        "kind": "ask",
        "text": "Would you take it on a road trip? Yes, no, or only as a joke.",
        "packs": None,
    },
    {
        "id": "gift-left",
        "kind": "ask",
        "text": "Would you gift this to the person on your left? Why or why not — don't name it.",
        "packs": None,
    },
    {
        "id": "disappear",
        "kind": "ask",
        "text": "If it vanished tomorrow, would you actually notice?",
        "packs": None,
    },
    {
        "id": "one-word-sense",
        "kind": "ask",
        "text": "One word for how it feels, sounds, or smells. Not its name.",
        "packs": None,
    },
    {
        "id": "last-time",
        "kind": "ask",
        "text": "Last time you encountered it — ten seconds. Skip any names.",
        "packs": None,
    },
    {
        "id": "kids-adults",
        "kind": "ask",
        "text": "Kids' thing, adults' thing, or both?",
        "packs": None,
    },
    {
        "id": "morning-night",
        "kind": "ask",
        "text": "Morning energy, night energy, or anytime?",
        "packs": None,
    },
    {
        "id": "cheap-pricey",
        "kind": "ask",
        "text": "Cheap, pricey, or you can't really buy it?",
        "packs": None,
    },
    {
        "id": "indoor-outdoor",
        "kind": "ask",
        "text": "Indoor, outdoor, or both?",
        "packs": None,
    },
    {
        "id": "overrated",
        "kind": "ask",
        "text": "What's overrated or underrated about it? Don't name it.",
        "packs": None,
    },
    {
        "id": "quiet-loud",
        "kind": "ask",
        "text": "Quiet thing, or loud thing?",
        "packs": None,
    },
    {
        "id": "fingers-like",
        "kind": "ask",
        "text": "Hold up 1–5 fingers for how much you like it, then defend that number.",
        "packs": None,
    },
    {
        "id": "first-look",
        "kind": "ask",
        "text": "First place you'd look for it? Keep the answer to a kind of place, not a name.",
        "packs": None,
    },
    {
        "id": "keep-or-toss",
        "kind": "ask",
        "text": "If you found this for free, would you keep it, pass it on, or leave it?",
        "packs": None,
    },
    # Actions — short, seated-table safe, no touching other people.
    {
        "id": "mime-5",
        "kind": "do",
        "text": "Mime encountering it for five seconds. No talking.",
        "packs": None,
    },
    {
        "id": "hand-size",
        "kind": "do",
        "text": "Show how big it is with your hands.",
        "packs": None,
    },
    {
        "id": "point-remind",
        "kind": "do",
        "text": "Point at something in the room that reminds you of it. Don't touch it, don't name it.",
        "packs": None,
    },
    {
        "id": "air-draw",
        "kind": "do",
        "text": "Draw its shape in the air.",
        "packs": None,
    },
    {
        "id": "sound",
        "kind": "do",
        "text": "Make a sound people associate with it. One shot.",
        "packs": None,
    },
    {
        "id": "face-vote",
        "kind": "do",
        "text": "Show with only your face whether you like it.",
        "packs": None,
    },
    {
        "id": "pass-imaginary",
        "kind": "do",
        "text": "Hand an imaginary version to the person on your right.",
        "packs": None,
    },
    {
        "id": "find-it",
        "kind": "do",
        "text": "Act out how you'd find it or get to it. Five seconds.",
        "packs": None,
    },
    {
        "id": "commercial",
        "kind": "do",
        "text": "Do a three-second commercial. Do not say the name.",
        "packs": None,
    },
    {
        "id": "pay-hands",
        "kind": "do",
        "text": "Raise a hand if you'd pay for it. Cross your arms if you wouldn't.",
        "packs": None,
    },
    {
        "id": "pretend-photo",
        "kind": "do",
        "text": "Pretend to take a photo of it, then react to the picture.",
        "packs": None,
    },
    {
        "id": "thumbs",
        "kind": "do",
        "text": "Thumbs up, sideways, or down — then freeze until the next speaker.",
        "packs": None,
    },
    {
        "id": "pose-freeze",
        "kind": "do",
        "text": "Count to three and freeze in a pose that fits it.",
        "packs": None,
    },
    {
        "id": "hum-montage",
        "kind": "do",
        "text": "Hum two seconds of a song you'd put in a montage of it.",
        "packs": None,
    },
    # Pack-flavored extras. Only dealt when this round's word came from that pack.
    {
        "id": "house-point-home",
        "kind": "do",
        "text": "Point toward where this would live in a home. If it's actually here, you may point at the real thing.",
        "packs": ("house",),
    },
    {
        "id": "house-mime-use",
        "kind": "do",
        "text": "Mime using it the way you actually would at home. No talking.",
        "packs": ("house",),
    },
    {
        "id": "food-bite",
        "kind": "do",
        "text": "Mime a bite or a sip. No flavor names.",
        "packs": ("food",),
    },
    {
        "id": "food-order-again",
        "kind": "ask",
        "text": "Would you order this again? Thumbs up, sideways, or down, then one reason.",
        "packs": ("food",),
    },
    {
        "id": "places-direction",
        "kind": "do",
        "text": "Point the direction you'd travel to get there. Guessing is allowed.",
        "packs": ("places",),
    },
    {
        "id": "places-arrive",
        "kind": "do",
        "text": "Mime the first thing you'd do when you arrived.",
        "packs": ("places",),
    },
    {
        "id": "animals-impression",
        "kind": "do",
        "text": "Three-second impression. Sound optional.",
        "packs": ("animals",),
    },
    {
        "id": "animals-move",
        "kind": "do",
        "text": "Show how it moves using just your arms.",
        "packs": ("animals",),
    },
    {
        "id": "party-vibe",
        "kind": "do",
        "text": "Act out the vibe: chill, hype, or chaotic.",
        "packs": ("party",),
    },
    {
        "id": "party-group",
        "kind": "do",
        "text": "Mime doing this with a group of friends. Five seconds.",
        "packs": ("party",),
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
    """Pick one shared prompt. Pack-tagged prompts only appear for that pack's words."""
    if mode == "off":
        return None
    pool = eligible_prompts(mode, pack_id)
    if not pool:
        return None
    blocked = set(exclude or ())
    unused = [prompt for prompt in pool if prompt["id"] not in blocked]
    return rng.choice(unused or pool)

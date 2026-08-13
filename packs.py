"""Starter word packs so a table can play without typing a full bank first.

Each entry is (word, fallback category). The fallback is used when AI clue
generation is unavailable, so imposters still get something to talk around.
"""

from __future__ import annotations

from typing import TypedDict


class Pack(TypedDict):
    id: str
    title: str
    blurb: str
    words: list[tuple[str, str]]


PACKS: dict[str, Pack] = {
    "house": {
        "id": "house",
        "title": "Around the house",
        "blurb": "Everyday objects you can point at without naming.",
        "words": [
            ("Coffee mug", "everyday objects"),
            ("Sofa", "everyday objects"),
            ("Umbrella", "everyday objects"),
            ("Backpack", "everyday objects"),
            ("Remote control", "everyday objects"),
            ("Toothbrush", "everyday objects"),
            ("House key", "everyday objects"),
            ("Desk lamp", "everyday objects"),
            ("Laundry basket", "everyday objects"),
            ("Water bottle", "everyday objects"),
            ("Sticky notes", "everyday objects"),
            ("Sunglasses", "everyday objects"),
        ],
    },
    "food": {
        "id": "food",
        "title": "Food & drink",
        "blurb": "Snacks, meals, and treats people have opinions about.",
        "words": [
            ("Tacos", "food and drink"),
            ("Sushi", "food and drink"),
            ("Pancakes", "food and drink"),
            ("Popcorn", "food and drink"),
            ("Mango", "food and drink"),
            ("Pretzel", "food and drink"),
            ("Ice cream", "food and drink"),
            ("Garlic bread", "food and drink"),
            ("Smoothie", "food and drink"),
            ("Nachos", "food and drink"),
            ("Waffle", "food and drink"),
            ("Hot chocolate", "food and drink"),
        ],
    },
    "places": {
        "id": "places",
        "title": "Places",
        "blurb": "Spots you could describe by what you do there.",
        "words": [
            ("Library", "places people go"),
            ("Airport", "places people go"),
            ("Beach", "places people go"),
            ("Gym", "places people go"),
            ("Grocery store", "places people go"),
            ("Movie theater", "places people go"),
            ("Camping tent", "places people go"),
            ("Amusement park", "places people go"),
            ("Coffee shop", "places people go"),
            ("Museum", "places people go"),
            ("Subway", "places people go"),
            ("Rooftop", "places people go"),
        ],
    },
    "animals": {
        "id": "animals",
        "title": "Animals",
        "blurb": "Creatures you can act out or compare.",
        "words": [
            ("Penguin", "animals"),
            ("Octopus", "animals"),
            ("Golden retriever", "animals"),
            ("Giraffe", "animals"),
            ("Owl", "animals"),
            ("Panda", "animals"),
            ("Crocodile", "animals"),
            ("Hummingbird", "animals"),
            ("Dolphin", "animals"),
            ("Hedgehog", "animals"),
            ("Flamingo", "animals"),
            ("Chameleon", "animals"),
        ],
    },
    "party": {
        "id": "party",
        "title": "Party mix",
        "blurb": "A mixed bag — the classic custom-imposter energy.",
        "words": [
            ("Karaoke", "fun things"),
            ("Fireworks", "fun things"),
            ("Board game", "fun things"),
            ("Birthday cake", "fun things"),
            ("Selfie", "fun things"),
            ("Roller coaster", "fun things"),
            ("Snowball", "fun things"),
            ("Treasure map", "fun things"),
            ("Robot", "fun things"),
            ("Pirate ship", "fun things"),
            ("Telescope", "fun things"),
            ("Superhero", "fun things"),
        ],
    },
}


def list_packs() -> list[dict[str, str | int]]:
    return [
        {
            "id": pack["id"],
            "title": pack["title"],
            "blurb": pack["blurb"],
            "wordCount": len(pack["words"]),
        }
        for pack in PACKS.values()
    ]


def get_pack(pack_id: str) -> Pack:
    pack = PACKS.get((pack_id or "").strip().lower())
    if pack is None:
        from engine import GameError

        raise GameError("Unknown word pack.")
    return pack

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
            ("Coffee mug", "kitchen items"),
            ("Sofa", "living-room furniture"),
            ("Umbrella", "rainy-day gear"),
            ("Backpack", "things you carry"),
            ("Remote control", "living-room gadgets"),
            ("Toothbrush", "bathroom routines"),
            ("House key", "things that open doors"),
            ("Desk lamp", "home lighting"),
            ("Laundry basket", "chores around the house"),
            ("Water bottle", "things you drink from"),
            ("Sticky notes", "office supplies"),
            ("Sunglasses", "accessories you wear"),
        ],
    },
    "food": {
        "id": "food",
        "title": "Food & drink",
        "blurb": "Snacks, meals, and treats people have opinions about.",
        "words": [
            ("Tacos", "handheld meals"),
            ("Sushi", "Japanese food"),
            ("Pancakes", "breakfast food"),
            ("Popcorn", "movie snacks"),
            ("Mango", "tropical fruit"),
            ("Pretzel", "salty snacks"),
            ("Ice cream", "frozen desserts"),
            ("Garlic bread", "side dishes"),
            ("Smoothie", "blended drinks"),
            ("Nachos", "shareable snacks"),
            ("Waffle", "breakfast food"),
            ("Hot chocolate", "warm drinks"),
        ],
    },
    "places": {
        "id": "places",
        "title": "Places",
        "blurb": "Spots you could describe by what you do there.",
        "words": [
            ("Library", "quiet public places"),
            ("Airport", "travel hubs"),
            ("Beach", "places by the water"),
            ("Gym", "places people work out"),
            ("Grocery store", "places you shop"),
            ("Movie theater", "places for a night out"),
            ("Camping tent", "outdoor overnight gear"),
            ("Amusement park", "places with rides"),
            ("Coffee shop", "places to meet up"),
            ("Museum", "places you look at things"),
            ("Subway", "public transportation"),
            ("Rooftop", "high-up places in a city"),
        ],
    },
    "animals": {
        "id": "animals",
        "title": "Animals",
        "blurb": "Creatures you can act out or compare.",
        "words": [
            ("Penguin", "birds that do not fly"),
            ("Octopus", "sea creatures"),
            ("Golden retriever", "popular pets"),
            ("Giraffe", "safari animals"),
            ("Owl", "nighttime animals"),
            ("Panda", "animals people find cute"),
            ("Crocodile", "reptiles"),
            ("Hummingbird", "tiny birds"),
            ("Dolphin", "marine mammals"),
            ("Hedgehog", "small spiky animals"),
            ("Flamingo", "pink animals"),
            ("Chameleon", "lizards"),
        ],
    },
    "party": {
        "id": "party",
        "title": "Party mix",
        "blurb": "A mixed bag — the classic custom-imposter energy.",
        "words": [
            ("Karaoke", "party activities"),
            ("Fireworks", "celebrations"),
            ("Board game", "things you play with friends"),
            ("Birthday cake", "party food"),
            ("Selfie", "phone habits"),
            ("Roller coaster", "theme-park rides"),
            ("Snowball", "winter fun"),
            ("Treasure map", "adventure stories"),
            ("Robot", "science fiction"),
            ("Pirate ship", "things from adventure movies"),
            ("Telescope", "things for looking far away"),
            ("Superhero", "comic-book characters"),
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

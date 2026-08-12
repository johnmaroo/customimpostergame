import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clues import (
    clue_contains_word,
    generate_category_clue,
    get_or_create_clue,
    load_env_file,
    parse_category,
)
from wordbank import WordBank


class ParseCategoryTests(unittest.TestCase):
    def test_plain_text(self) -> None:
        self.assertEqual(parse_category("  kitchen tools  "), "kitchen tools")

    def test_json_object(self) -> None:
        self.assertEqual(
            parse_category('{"category": "famous landmarks"}'),
            "famous landmarks",
        )

    def test_fenced_json(self) -> None:
        raw = '```json\n{"category": "board games"}\n```'
        self.assertEqual(parse_category(raw), "board games")

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_category("   ")


class GenerateClueTests(unittest.TestCase):
    def test_uses_injected_complete_and_parses_json(self) -> None:
        calls: list[dict] = []

        def complete(*, messages, model, api_key=None):
            calls.append({"messages": messages, "model": model, "api_key": api_key})
            return json.dumps({"category": "citrus fruit"})

        clue = generate_category_clue("orange", complete=complete, model="openai/gpt-5.4-mini")
        self.assertEqual(clue, "citrus fruit")
        self.assertEqual(len(calls), 1)
        self.assertIn("orange", calls[0]["messages"][1]["content"])

    def test_retries_when_clue_contains_the_word(self) -> None:
        responses = iter(
            [
                json.dumps({"category": "something about bananas"}),
                json.dumps({"category": "tropical fruit"}),
            ]
        )

        def complete(*, messages, model, api_key=None):
            return next(responses)

        clue = generate_category_clue("banana", complete=complete)
        self.assertEqual(clue, "tropical fruit")

    def test_raises_if_retry_still_contains_word(self) -> None:
        def complete(*, messages, model, api_key=None):
            return json.dumps({"category": "banana split toppings"})

        with self.assertRaises(ValueError):
            generate_category_clue("banana", complete=complete)

    def test_clue_contains_word(self) -> None:
        self.assertTrue(clue_contains_word("a kind of Apple", "apple"))
        self.assertFalse(clue_contains_word("orchard fruit", "apple"))
        self.assertFalse(clue_contains_word("party games", "art"))
        self.assertTrue(clue_contains_word("something about bananas", "banana"))


class GetOrCreateClueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bank = WordBank(Path(self._tmp.name) / "wordbank.db")
        self.bank.add_word("Eiffel Tower")

    def tearDown(self) -> None:
        self.bank.close()
        self._tmp.cleanup()

    def test_generates_and_caches(self) -> None:
        calls = []

        def generate(word: str) -> str:
            calls.append(word)
            return "famous landmarks"

        first = get_or_create_clue(self.bank, "eiffel tower", generate=generate)
        second = get_or_create_clue(self.bank, "Eiffel Tower", generate=generate)
        self.assertEqual(first, "famous landmarks")
        self.assertEqual(second, "famous landmarks")
        self.assertEqual(calls, ["eiffel tower"])

    def test_returns_none_when_generation_fails(self) -> None:
        def generate(word: str) -> str:
            raise RuntimeError("no key")

        self.assertIsNone(get_or_create_clue(self.bank, "Eiffel Tower", generate=generate))
        self.assertIsNone(self.bank.get_clue("Eiffel Tower"))


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_does_not_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("AI_GATEWAY_API_KEY=from-file\nOTHER=xyz\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"AI_GATEWAY_API_KEY": "already-set"},
                clear=False,
            ):
                os.environ.pop("OTHER", None)
                try:
                    load_env_file(path)
                    self.assertEqual(os.environ["AI_GATEWAY_API_KEY"], "already-set")
                    self.assertEqual(os.environ["OTHER"], "xyz")
                finally:
                    os.environ.pop("OTHER", None)


if __name__ == "__main__":
    unittest.main()

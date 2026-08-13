import random
import re
import unittest

from packs import PACKS
from prompts import PROMPTS, eligible_prompts, pick_prompt, prompt_view


class PromptDeckTests(unittest.TestCase):
    def test_ids_are_unique(self) -> None:
        ids = [prompt["id"] for prompt in PROMPTS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_kinds_are_ask_or_do(self) -> None:
        for prompt in PROMPTS:
            self.assertIn(prompt["kind"], {"ask", "do"})
            self.assertTrue(prompt["text"].strip())

    def test_prompts_do_not_name_pack_words(self) -> None:
        secrets = []
        for pack in PACKS.values():
            secrets.extend(word for word, _clue in pack["words"])
        haystack = "\n".join(prompt["text"] for prompt in PROMPTS)
        for word in secrets:
            pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
            self.assertIsNone(
                re.search(pattern, haystack, re.IGNORECASE),
                f"prompt deck names the secret {word!r}",
            )

    def test_off_mode_returns_none(self) -> None:
        self.assertIsNone(pick_prompt(random.Random(0), mode="off"))

    def test_ask_mode_only_questions(self) -> None:
        rng = random.Random(1)
        for _ in range(20):
            prompt = pick_prompt(rng, mode="ask")
            self.assertIsNotNone(prompt)
            self.assertEqual(prompt["kind"], "ask")
            self.assertIsNone(prompt["packs"])

    def test_do_mode_only_actions(self) -> None:
        rng = random.Random(2)
        for _ in range(20):
            prompt = pick_prompt(rng, mode="do")
            self.assertIsNotNone(prompt)
            self.assertEqual(prompt["kind"], "do")

    def test_pack_prompts_only_when_that_pack_is_in_play(self) -> None:
        generic = {prompt["id"] for prompt in eligible_prompts("mix", None)}
        house = {prompt["id"] for prompt in eligible_prompts("mix", "house")}
        self.assertTrue(generic)
        self.assertTrue(house - generic)
        self.assertTrue(all(not p["packs"] or "house" in p["packs"] for p in eligible_prompts("mix", "house")))
        self.assertNotIn("animals-impression", generic)
        self.assertIn("animals-impression", {p["id"] for p in eligible_prompts("mix", "animals")})

    def test_exclude_avoids_recent_ids(self) -> None:
        pool = eligible_prompts("ask", None)
        keep = pool[0]
        blocked = [prompt["id"] for prompt in pool if prompt["id"] != keep["id"]]
        picked = pick_prompt(random.Random(0), mode="ask", exclude=blocked)
        self.assertEqual(picked["id"], keep["id"])

    def test_prompt_view_omits_pack_tags(self) -> None:
        view = prompt_view(PROMPTS[0])
        self.assertEqual(set(view), {"id", "kind", "text"})


if __name__ == "__main__":
    unittest.main()

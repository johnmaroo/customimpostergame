import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gameAIRevised import add_word_to_session, imposter_message, play_round
from wordbank import WordBank


class ImposterMessageTests(unittest.TestCase):
    def test_includes_category_when_present(self) -> None:
        self.assertEqual(
            imposter_message("board games"),
            "You are the Imposter.\nCategory: board games",
        )

    def test_without_category(self) -> None:
        self.assertEqual(imposter_message(None), "You are the Imposter.")


class SessionAndRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bank = WordBank(Path(self._tmp.name) / "wordbank.db")

    def tearDown(self) -> None:
        self.bank.close()
        self._tmp.cleanup()

    def test_add_word_to_session_saves_and_creates_clue(self) -> None:
        session: list[str] = []
        added = add_word_to_session(
            self.bank,
            session,
            "  Monopoly  ",
            generate_clue=lambda word: "board games",
        )
        self.assertTrue(added)
        self.assertEqual(session, ["Monopoly"])
        self.assertEqual(self.bank.all_words(), ["Monopoly"])
        self.assertEqual(self.bank.get_clue("monopoly"), "board games")

        added_again = add_word_to_session(
            self.bank,
            session,
            "monopoly",
            generate_clue=lambda word: "should not be called",
        )
        self.assertFalse(added_again)
        self.assertEqual(session, ["Monopoly"])

    def test_play_round_sends_clue_to_imposters_and_word_to_faithfuls(self) -> None:
        self.bank.add_word("toaster")
        self.bank.set_clue("toaster", "kitchen appliances")
        wordbank = ["toaster"]
        sent: list[tuple[str, str]] = []

        def send(recipient, message, service_hint=None):
            sent.append((recipient, message))

        with patch("gameAIRevised.random.choice", return_value="toaster"), patch(
            "gameAIRevised.random.sample", return_value=["111"]
        ), patch("gameAIRevised.clear_terminal"):
            chosen = play_round(
                ["111", "222", "333"],
                wordbank,
                1,
                self.bank,
                send=send,
            )

        self.assertEqual(chosen, "toaster")
        self.assertEqual(wordbank, [])
        messages = dict(sent)
        self.assertEqual(messages["111"], "You are the Imposter.\nCategory: kitchen appliances")
        self.assertEqual(messages["222"], "toaster")
        self.assertEqual(messages["333"], "toaster")

    def test_play_round_without_clue_still_notifies_imposter(self) -> None:
        self.bank.add_word("violin")
        wordbank = ["violin"]
        sent: list[tuple[str, str]] = []

        def send(recipient, message, service_hint=None):
            sent.append((recipient, message))

        with patch("gameAIRevised.random.choice", return_value="violin"), patch(
            "gameAIRevised.random.sample", return_value=["111"]
        ), patch("gameAIRevised.clear_terminal"):
            play_round(
                ["111", "222"],
                wordbank,
                1,
                self.bank,
                send=send,
                generate_clue=lambda word: (_ for _ in ()).throw(RuntimeError("offline")),
            )

        messages = dict(sent)
        self.assertEqual(messages["111"], "You are the Imposter.")
        self.assertEqual(messages["222"], "violin")


if __name__ == "__main__":
    unittest.main()

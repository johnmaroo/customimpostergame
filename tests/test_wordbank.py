import tempfile
import unittest
from pathlib import Path

from wordbank import WordBank


class WordBankTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "wordbank.db"
        self.bank = WordBank(self.db_path)

    def tearDown(self) -> None:
        self.bank.close()
        self._tmp.cleanup()

    def test_add_word_persists_and_skips_duplicates(self) -> None:
        self.assertTrue(self.bank.add_word("  Apple pie  "))
        self.assertFalse(self.bank.add_word("apple pie"))
        self.assertEqual(self.bank.all_words(), ["Apple pie"])

        reopened = WordBank(self.db_path)
        try:
            self.assertEqual(reopened.all_words(), ["Apple pie"])
        finally:
            reopened.close()

    def test_empty_word_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.bank.add_word("   ")

    def test_category_clue_round_trip(self) -> None:
        self.bank.add_word("toaster")
        self.assertIsNone(self.bank.get_clue("toaster"))
        self.bank.set_clue("TOASTER", "kitchen appliances")
        self.assertEqual(self.bank.get_clue("toaster"), "kitchen appliances")

    def test_set_clue_requires_existing_word(self) -> None:
        with self.assertRaises(KeyError):
            self.bank.set_clue("missing", "somewhere")

    def test_mark_used_updates_timestamp(self) -> None:
        self.bank.add_word("banana")
        self.bank.mark_used("Banana")
        row = self.bank.conn.execute(
            "SELECT last_used_at FROM words WHERE word_normalized = ?",
            ("banana",),
        ).fetchone()
        self.assertIsNotNone(row["last_used_at"])

    def test_remove_word_and_unused_order(self) -> None:
        self.assertTrue(self.bank.add_word("banana"))
        self.assertTrue(self.bank.add_word("mango"))
        self.bank.mark_used("banana")
        self.assertEqual(self.bank.unused_words()[0], "mango")
        self.assertTrue(self.bank.remove_word("Banana"))
        self.assertEqual(self.bank.all_words(), ["mango"])
        self.assertFalse(self.bank.remove_word("missing"))


if __name__ == "__main__":
    unittest.main()

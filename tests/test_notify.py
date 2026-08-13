import unittest

from engine import GameError
from notify import invite_message, mask_phone, normalize_phone, qr_svg, sms_url


class PhoneTests(unittest.TestCase):
    def test_us_ten_digit(self) -> None:
        self.assertEqual(normalize_phone("(555) 123-4567"), "+15551234567")
        self.assertEqual(normalize_phone("5551234567"), "+15551234567")

    def test_already_e164(self) -> None:
        self.assertEqual(normalize_phone("+44 7700 900123"), "+447700900123")

    def test_rejects_short(self) -> None:
        with self.assertRaises(GameError):
            normalize_phone("555-12")

    def test_mask(self) -> None:
        self.assertEqual(mask_phone("+15551234567"), "•••4567")

    def test_sms_url_contains_body(self) -> None:
        url = sms_url("+15551234567", "Join Imposter room ABCD: http://example/join/ABCD")
        self.assertTrue(url.startswith("sms:+15551234567?"))
        self.assertIn("body=", url)

    def test_invite_message(self) -> None:
        text = invite_message("KNTQ", "http://192.168.1.9:8765/join/KNTQ", "Maya")
        self.assertIn("KNTQ", text)
        self.assertIn("Maya", text)
        self.assertIn("http://192.168.1.9:8765/join/KNTQ", text)

    def test_qr_svg_encodes_url(self) -> None:
        svg = qr_svg("http://192.168.1.9:8765/join/KNTQ")
        self.assertTrue(svg.lstrip().startswith("<svg"))
        self.assertIn("qrline", svg)


if __name__ == "__main__":
    unittest.main()

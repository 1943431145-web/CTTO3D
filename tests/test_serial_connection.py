import unittest

from ctto3d import serial_connection


class SerialHexTests(unittest.TestCase):
    def test_hex_parser_accepts_spaced_compact_and_prefixed_input(self):
        expected = bytes.fromhex("AA 13 00 FF")
        self.assertEqual(serial_connection.parse_hex_bytes("AA 13 00 FF"), expected)
        self.assertEqual(serial_connection.parse_hex_bytes("AA1300ff"), expected)
        self.assertEqual(
            serial_connection.parse_hex_bytes("0xAA, 0x13, 0x00, 0xFF"),
            expected,
        )

    def test_hex_parser_rejects_partial_or_invalid_bytes(self):
        with self.assertRaises(ValueError):
            serial_connection.parse_hex_bytes("AA 1")
        with self.assertRaises(ValueError):
            serial_connection.parse_hex_bytes("AA GG")
        with self.assertRaises(ValueError):
            serial_connection.parse_hex_bytes("")


if __name__ == "__main__":
    unittest.main()

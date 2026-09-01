import math
import unittest

from ctto3d import rtx_telemetry as protocol


def _s16(value):
    value = int(value) & 0xFFFF
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


def _frame(*, temp=365, pitch=100, roll=-100, yaw=0,
           mag=(300, -400, 120), status=0, committed=1):
    payload = (
        bytes((status, (temp >> 8) & 0xFF, temp & 0xFF))
        + _s16(pitch) + _s16(roll) + _s16(yaw)
        + _s16(mag[0]) + _s16(mag[1]) + _s16(mag[2])
        + bytes((committed,))
    )
    body = bytes((protocol.RTX_DATA_COMMAND,)) + payload
    return bytes((protocol.FRAME_HEAD,)) + body + bytes((
        protocol.xor_checksum(body), protocol.FRAME_TAIL
    ))


class RtxTelemetryTests(unittest.TestCase):
    def test_protocol_document_example(self):
        frame = bytes.fromhex(
            "AA 13 00 01 6D 00 64 FF 9C 00 00 "
            "00 00 00 00 00 00 01 79 BB"
        )
        data = protocol.decode_rtx_data(frame)
        self.assertEqual(data["rod_temp_c"], 36.5)
        self.assertEqual(data["pitch_deg"], 10.0)
        self.assertEqual(data["roll_deg"], -10.0)
        self.assertEqual(data["yaw_deg"], 0.0)
        self.assertEqual(data["magnetic_ut"], (0.0, 0.0, 0.0))
        self.assertIsNone(data["magnetic_heading_deg"])

    def test_signed_magnetometer_and_direction(self):
        data = protocol.decode_rtx_data(_frame())
        self.assertEqual(data["mag_raw"], (300, -400, 120))
        self.assertEqual(data["magnetic_ut"], (30.0, -40.0, 12.0))
        self.assertAlmostEqual(data["magnetic_magnitude_ut"], 51.4198, places=3)
        self.assertAlmostEqual(
            math.sqrt(sum(v * v for v in data["magnetic_direction"])), 1.0
        )
        self.assertIsNotNone(data["magnetic_heading_deg"])

    def test_stream_parser_handles_noise_fragmentation_and_resync(self):
        parser = protocol.RtxDataStreamParser()
        good = _frame()
        frames, issues = parser.feed(b"debug line\r\n" + good[:7])
        self.assertEqual((frames, issues), ([], []))
        frames, issues = parser.feed(good[7:])
        self.assertEqual(issues, [])
        self.assertEqual(frames, [good])

        bad = bytearray(good)
        bad[-2] ^= 0x01
        frames, issues = parser.feed(bytes(bad) + good)
        self.assertEqual(frames, [good])
        self.assertTrue(any("校验" in issue for issue in issues))

    def test_data_ack_matches_rtx_protocol(self):
        self.assertEqual(protocol.build_data_ack(), bytes.fromhex("AA 93 93 BB"))


if __name__ == "__main__":
    unittest.main()

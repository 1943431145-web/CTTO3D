import unittest

from ctto3d import hw100_protocol as protocol


class Hw100ProtocolTests(unittest.TestCase):
    def test_parameter_frame_matches_protocol_example(self):
        parameters = protocol.ControlParameters(
            power_w=50,
            work_time_s=600,
            mode=0,
            bypass_alarm_deci_c=500,
            rod_alarm_deci_c=450,
        )
        self.assertEqual(
            protocol.build_parameter_set(parameters),
            bytes.fromhex("AA 20 32 02 58 00 01 F4 01 C2 7E BB"),
        )

    def test_simple_request_and_ack_examples(self):
        self.assertEqual(protocol.build_start(), bytes.fromhex("AA 21 21 BB"))
        self.assertEqual(protocol.build_stop(), bytes.fromhex("AA 22 22 BB"))
        self.assertEqual(
            protocol.build_pump_control(True), bytes.fromhex("AA 23 01 22 BB")
        )
        self.assertEqual(
            protocol.build_status_query(), bytes.fromhex("AA 24 24 BB")
        )
        self.assertEqual(
            protocol.build_notification_ack(0x05), bytes.fromhex("AA A6 05 A3 BB")
        )

    def test_manual_tx_direction_accepts_requests_only(self):
        self.assertIsNone(
            protocol.structural_error(
                bytes.fromhex("AA 24 24 BB"), protocol.PC_OUTBOUND_LENGTHS
            )
        )
        self.assertIn(
            "未知命令字",
            protocol.structural_error(
                bytes.fromhex("AA A0 00 A0 BB"), protocol.PC_OUTBOUND_LENGTHS
            ),
        )

    def test_decode_status_snapshot_example(self):
        frame = bytes.fromhex(
            "AA A4 32 02 58 00 01 F4 01 C2 01 2C 01 6D "
            "03 01 01 03 07 08 00 00 B4 BB"
        )
        self.assertIsNone(protocol.structural_error(frame, protocol.PC_INBOUND_LENGTHS))
        self.assertIsNone(protocol.semantic_error(frame))
        snapshot = protocol.decode_snapshot(frame)
        self.assertEqual(snapshot["control"].power_w, 50)
        self.assertEqual(snapshot["control"].work_time_s, 600)
        self.assertEqual(snapshot["bypass_temp_c"], 30.0)
        self.assertEqual(snapshot["rod_temp_c"], 36.5)
        self.assertTrue(snapshot["microwave_on"])
        self.assertTrue(snapshot["pump_on"])
        self.assertTrue(snapshot["needle_paired"])
        self.assertEqual(snapshot["needle_remaining_time_s"], 1800)

    def test_decode_realtime_signed_values(self):
        frame = bytes.fromhex(
            "AA 25 00 01 6D 00 64 FF 9C 00 00 00 00 00 00 "
            "00 00 32 01 2C 03 00 52 BB"
        )
        data = protocol.decode_realtime(frame)
        self.assertEqual(data["rod_temp_c"], 36.5)
        self.assertEqual(data["pitch_deg"], 10.0)
        self.assertEqual(data["roll_deg"], -10.0)
        self.assertEqual(data["yaw_deg"], 0.0)
        self.assertEqual((data["mag_x"], data["mag_y"], data["mag_z"]), (0, 0, 0))

    def test_notification_snapshot_uses_same_offsets(self):
        frame = bytes.fromhex(
            "AA 26 32 02 58 00 01 F4 01 C2 01 2C 01 6D "
            "00 01 01 03 07 08 00 10 05 20 BB"
        )
        snapshot = protocol.decode_snapshot(frame)
        self.assertEqual(snapshot["notification_sequence"], 0x05)
        self.assertEqual(snapshot["alarm_flag"], 0x10)
        self.assertFalse(snapshot["microwave_on"])
        self.assertTrue(snapshot["needle_paired"])

    def test_stream_parser_handles_fragmentation_and_concatenation(self):
        parser = protocol.FrameStreamParser()
        first = bytes.fromhex("AA A0 00 A0 BB")
        second = bytes.fromhex("AA A1 82 23 BB")
        frames, issues = parser.feed(first[:3], now=1.0)
        self.assertEqual((frames, issues), ([], []))
        frames, issues = parser.feed(first[3:] + second, now=1.01)
        self.assertEqual(issues, [])
        self.assertEqual(frames, [first, second])

    def test_stream_parser_discards_only_bad_head_candidate(self):
        parser = protocol.FrameStreamParser()
        good = bytes.fromhex("AA A0 00 A0 BB")
        bad = bytearray(good)
        bad[-2] ^= 0x01
        frames, issues = parser.feed(bytes(bad) + good, now=1.0)
        self.assertEqual(frames, [good])
        self.assertTrue(any("校验" in issue for issue in issues))

    def test_stream_parser_expires_partial_frame_after_20ms(self):
        parser = protocol.FrameStreamParser()
        good = bytes.fromhex("AA A0 00 A0 BB")
        frames, _ = parser.feed(bytes.fromhex("AA A4 32"), now=1.0)
        self.assertEqual(frames, [])
        frames, issues = parser.feed(good, now=1.021)
        self.assertEqual(frames, [good])
        self.assertTrue(any("超时" in issue for issue in issues))

    def test_invalid_notification_is_rejected_semantically(self):
        # Valid structure/checksum, but output mode 2 is outside the V1 enum.
        payload = bytes.fromhex(
            "32 02 58 02 01 F4 01 C2 01 2C 01 6D "
            "00 01 01 03 07 08 00 00 05"
        )
        frame = protocol.build_frame(0x26, payload)
        self.assertIn("输出模式", protocol.semantic_error(frame))


if __name__ == "__main__":
    unittest.main()

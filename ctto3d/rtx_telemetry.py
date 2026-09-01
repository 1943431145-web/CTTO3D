"""RTX -> upper-computer telemetry protocol helpers.

The RTX firmware in ``RF_Project_PC_Connect`` forwards the TRX sensor sample
as a fixed 20-byte ``0x13`` frame::

    AA 13 status temp*10 pitch*10 roll*10 yaw*10
          magX*10 magY*10 magZ*10 committed checksum BB

All multi-byte fields are big-endian.  Attitude and magnetometer fields are
signed 16-bit values; the firmware stores angles in 0.1 degree and magnetic
field components in 0.1 microtesla.  This module intentionally does not
integrate any sensor value into a position.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple


FRAME_HEAD = 0xAA
FRAME_TAIL = 0xBB
RTX_DATA_COMMAND = 0x13
RTX_DATA_FRAME_LENGTH = 20


def xor_checksum(values: Iterable[int]) -> int:
    checksum = 0
    for value in values:
        checksum ^= int(value) & 0xFF
    return checksum


def _u16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def _i16(data: bytes, offset: int) -> int:
    value = _u16(data, offset)
    return value - 0x10000 if value & 0x8000 else value


def frame_error(frame: bytes) -> Optional[str]:
    """Return a readable structural error, or ``None`` for a valid frame."""
    if len(frame) != RTX_DATA_FRAME_LENGTH:
        return "RTX 0x13 帧长度应为 20 字节，实际为 %d" % len(frame)
    if frame[0] != FRAME_HEAD:
        return "RTX 0x13 帧头不是 0xAA"
    if frame[1] != RTX_DATA_COMMAND:
        return "不是 RTX 0x13 数据上报帧"
    if frame[-1] != FRAME_TAIL:
        return "RTX 0x13 帧尾不是 0xBB"
    if xor_checksum(frame[1:-2]) != frame[-2]:
        return "RTX 0x13 异或校验失败"
    return None


def magnetic_heading_deg(
    mag_x: float,
    mag_y: float,
    mag_z: float,
    pitch_deg: float,
    roll_deg: float,
) -> Optional[float]:
    """Return the tilt-compensated magnetic heading in the firmware convention.

    The calculation mirrors ``imu_9axis_init_from_acc_mag`` in the connect
    firmware.  It is a magnetic indication only: no declination correction is
    applied, so the result is not geographic true north.
    """
    magnitude = math.sqrt(mag_x * mag_x + mag_y * mag_y + mag_z * mag_z)
    if magnitude < 1.0e-9:
        return None
    pitch = math.radians(float(pitch_deg))
    roll = math.radians(float(roll_deg))
    sr, cr = math.sin(roll), math.cos(roll)
    sp, cp = math.sin(pitch), math.cos(pitch)
    level_x = mag_x * cp + mag_y * sr * sp + mag_z * cr * sp
    level_y = mag_y * cr - mag_z * sr
    if level_x * level_x + level_y * level_y < 1.0e-12:
        return None
    return math.degrees(math.atan2(-level_y, level_x)) % 360.0


def decode_rtx_data(frame: bytes) -> Dict[str, object]:
    """Validate and decode one RTX ``0x13`` data frame."""
    frame = bytes(frame)
    error = frame_error(frame)
    if error:
        raise ValueError(error)

    mag_raw = (_i16(frame, 11), _i16(frame, 13), _i16(frame, 15))
    magnetic_ut = tuple(value / 10.0 for value in mag_raw)
    magnitude_ut = math.sqrt(sum(value * value for value in magnetic_ut))
    direction = None
    if magnitude_ut > 1.0e-9:
        direction = tuple(value / magnitude_ut for value in magnetic_ut)

    pitch_deg = _i16(frame, 5) / 10.0
    roll_deg = _i16(frame, 7) / 10.0
    yaw_deg = _i16(frame, 9) / 10.0
    return {
        "needle_state": frame[2],
        "rod_temp_c": _u16(frame, 3) / 10.0,
        "pitch_deg": pitch_deg,
        "roll_deg": roll_deg,
        "yaw_deg": yaw_deg,
        "mag_raw": mag_raw,
        "magnetic_ut": magnetic_ut,
        "magnetic_magnitude_ut": magnitude_ut,
        "magnetic_direction": direction,
        "magnetic_heading_deg": magnetic_heading_deg(
            *magnetic_ut, pitch_deg, roll_deg
        ),
        "committed": frame[17],
    }


def build_data_ack() -> bytes:
    """Build the HW100/upper-computer acknowledgement for an RTX 0x13 frame."""
    return bytes((FRAME_HEAD, 0x93, 0x93, FRAME_TAIL))


class RtxDataStreamParser:
    """Incrementally extract valid RTX ``0x13`` frames from a noisy byte stream.

    Debug text and other protocol commands may share the UART during bring-up.
    Only the exact ``AA 13 ... checksum BB`` sequence is retained.  A trailing
    partial candidate is kept for the next ``feed`` call.
    """

    _PREFIX = bytes((FRAME_HEAD, RTX_DATA_COMMAND))

    def __init__(self, max_buffer: int = 4096):
        self.max_buffer = max(RTX_DATA_FRAME_LENGTH, int(max_buffer))
        self.buffer = bytearray()

    def reset(self) -> None:
        self.buffer.clear()

    def feed(self, data: bytes) -> Tuple[List[bytes], List[str]]:
        if data:
            self.buffer.extend(data)
        frames: List[bytes] = []
        issues: List[str] = []

        while True:
            start = self.buffer.find(self._PREFIX)
            if start < 0:
                # Keep one possible split prefix byte (0xAA), discard all
                # ordinary debug text without reporting it as protocol noise.
                keep_head = bool(self.buffer and self.buffer[-1] == FRAME_HEAD)
                self.buffer[:] = bytes((FRAME_HEAD,)) if keep_head else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < RTX_DATA_FRAME_LENGTH:
                break

            candidate = bytes(self.buffer[:RTX_DATA_FRAME_LENGTH])
            error = frame_error(candidate)
            if error is None:
                frames.append(candidate)
                del self.buffer[:RTX_DATA_FRAME_LENGTH]
                continue
            issues.append(error)
            # Discard only this head byte, then search for the next AA 13.
            del self.buffer[0]

        if len(self.buffer) > self.max_buffer:
            del self.buffer[:-RTX_DATA_FRAME_LENGTH]
            issues.append("RTX 接收缓冲区溢出，已重新同步")
        return frames, issues

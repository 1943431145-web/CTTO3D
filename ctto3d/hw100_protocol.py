"""PC <-> HW100 V1 binary protocol helpers.

This module deliberately contains no Qt code.  It is the single place where
frame lengths, checksums, field ranges and byte layouts from
``通信协议_PC-HW100_V1.md`` are encoded.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Tuple


FRAME_HEAD = 0xAA
FRAME_TAIL = 0xBB
INTER_BYTE_TIMEOUT_S = 0.020

# All commands that are legal on the PC <-> HW100 UART.  Keeping both
# directions here lets the stream parser reject a command from another link in
# the protocol family immediately instead of waiting for an invented length.
FRAME_LENGTHS: Dict[int, int] = {
    0x20: 12,
    0xA0: 5,
    0x21: 4,
    0xA1: 5,
    0x22: 4,
    0xA2: 10,
    0x23: 5,
    0xA3: 5,
    0x24: 4,
    0xA4: 24,
    0x25: 24,
    0x26: 25,
    0xA6: 5,
}

PC_INBOUND_LENGTHS: Dict[int, int] = {
    command: FRAME_LENGTHS[command]
    for command in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0x25, 0x26)
}

# Frames that the PC is allowed to send to HW100.  This direction-specific
# table is also used by the manual HEX monitor so a pasted HW100 response
# cannot accidentally be transmitted back to the device.
PC_OUTBOUND_LENGTHS: Dict[int, int] = {
    command: FRAME_LENGTHS[command]
    for command in (0x20, 0x21, 0x22, 0x23, 0x24, 0xA6)
}

RESULT_TEXT = {
    0x00: "成功",
    0x01: "消融针可用次数已耗尽",
    0x02: "消融针可用时间已耗尽",
    0x03: "消融针次数与时间额度均已耗尽",
    0x04: "消融针 Flash 数据无效或持久化失败",
    0x05: "消融针次数耗尽且 Flash 数据异常",
    0x06: "消融针时间耗尽且 Flash 数据异常",
    0x07: "消融针额度耗尽且 Flash 数据异常",
    0x80: "消融针无应答，RF 重发已耗尽",
    0x81: "未检测到唯一目标针，配对尚未完成或缓存已过期",
    0x82: "蠕动泵未启动",
    0x83: "当前状态不允许执行",
    0x84: "请求字段越界或枚举值非法",
    0x85: "启动事务已被停止或取消动作抢占",
    0x86: "上一次会话所属针已丢失或更换，结算永久无法确认",
    0x87: "启动批准未送达 RTX，启动未执行",
    0x88: "针的提交结果未确认，微波未输出且可能已扣一次",
    0x89: "启动正在处理中，重复请求已忽略",
    0x8A: "针已提交扣费，但最终安全联锁复核失败",
    0x8B: "上一笔停止或针端结算仍在处理中",
    0x8C: "HW100 等待 RTX 启动结果超时，微波未输出",
}

ALARM_BITS = (
    (0x01, "旁路针过温"),
    (0x02, "针杆过温"),
    (0x04, "消融针未配对或缓存无效"),
    (0x08, "消融针空载"),
    (0x10, "蠕动泵泵盖打开"),
    (0x20, "旁温针未连接"),
    (0x40, "脚踏开关未连接"),
    (0x80, "针通信失联"),
)


def xor_checksum(values: Iterable[int]) -> int:
    checksum = 0
    for value in values:
        checksum ^= int(value) & 0xFF
    return checksum


def build_frame(command: int, payload: bytes = b"") -> bytes:
    """Build one AA/BB frame and verify its fixed length when known."""
    command = int(command) & 0xFF
    body = bytes([command]) + bytes(payload)
    frame = bytes([FRAME_HEAD]) + body + bytes([xor_checksum(body), FRAME_TAIL])
    expected = FRAME_LENGTHS.get(command)
    if expected is not None and len(frame) != expected:
        raise ValueError(
            "command 0x%02X requires %d bytes, got %d"
            % (command, expected, len(frame))
        )
    return frame


def _u16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def _i16(data: bytes, offset: int) -> int:
    value = _u16(data, offset)
    return value - 0x10000 if value & 0x8000 else value


def _put_u16(value: int) -> bytes:
    value = int(value)
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


@dataclass(frozen=True)
class ControlParameters:
    """The eight-byte control block shared by 0x20, 0xA4 and 0x26."""

    power_w: int
    work_time_s: int
    mode: int
    bypass_alarm_deci_c: int
    rod_alarm_deci_c: int

    def validation_error(self) -> Optional[str]:
        if not 0 <= int(self.power_w) <= 100:
            return "微波功率必须在 0~100 W"
        if not 0 <= int(self.work_time_s) <= 1800:
            return "工作时间必须在 0~1800 s"
        if int(self.mode) not in (0, 1):
            return "输出模式必须为连续或脉冲"
        if not 150 <= int(self.bypass_alarm_deci_c) <= 600:
            return "旁路报警阈值必须在 15.0~60.0 ℃"
        if not 400 <= int(self.rod_alarm_deci_c) <= 450:
            return "杆温报警阈值必须在 40.0~45.0 ℃"
        return None

    def to_bytes(self) -> bytes:
        error = self.validation_error()
        if error:
            raise ValueError(error)
        return b"".join(
            (
                bytes([int(self.power_w)]),
                _put_u16(self.work_time_s),
                bytes([int(self.mode)]),
                _put_u16(self.bypass_alarm_deci_c),
                _put_u16(self.rod_alarm_deci_c),
            )
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "ControlParameters":
        if len(data) != 8:
            raise ValueError("控制参数块必须为 8 字节")
        value = cls(
            power_w=data[0],
            work_time_s=_u16(data, 1),
            mode=data[3],
            bypass_alarm_deci_c=_u16(data, 4),
            rod_alarm_deci_c=_u16(data, 6),
        )
        error = value.validation_error()
        if error:
            raise ValueError(error)
        return value

    def changed(self, **values: int) -> "ControlParameters":
        return replace(self, **values)


def build_parameter_set(parameters: ControlParameters) -> bytes:
    return build_frame(0x20, parameters.to_bytes())


def build_start() -> bytes:
    return build_frame(0x21)


def build_stop() -> bytes:
    return build_frame(0x22)


def build_pump_control(enabled: bool) -> bytes:
    return build_frame(0x23, bytes([1 if enabled else 0]))


def build_status_query() -> bytes:
    return build_frame(0x24)


def build_notification_ack(sequence: int) -> bytes:
    return build_frame(0xA6, bytes([int(sequence) & 0xFF]))


def structural_error(
    frame: bytes, lengths: Optional[Dict[int, int]] = None
) -> Optional[str]:
    lengths = FRAME_LENGTHS if lengths is None else lengths
    if len(frame) < 4:
        return "帧长度小于最短长度"
    if frame[0] != FRAME_HEAD:
        return "帧头不是 0xAA"
    command = frame[1]
    expected = lengths.get(command)
    if expected is None:
        return "未知命令字 0x%02X" % command
    if len(frame) != expected:
        return "命令 0x%02X 长度应为 %d，实际为 %d" % (
            command,
            expected,
            len(frame),
        )
    if frame[-1] != FRAME_TAIL:
        return "帧尾不是 0xBB"
    if xor_checksum(frame[1:-2]) != frame[-2]:
        return "异或校验错误"
    return None


def _run_state_error(run_state: int) -> Optional[str]:
    if run_state & 0xE0:
        return "运行状态保留位非 0"
    main_states = run_state & 0x0D  # bit0 output, bit2 start, bit3 settlement
    if main_states and main_states & (main_states - 1):
        return "微波输出、启动处理中和结算处理中状态互斥"
    return None


def _snapshot_error(snapshot: bytes) -> Optional[str]:
    if len(snapshot) != 20:
        return "状态快照载荷必须为 20 字节"
    try:
        ControlParameters.from_bytes(snapshot[:8])
    except ValueError as exc:
        return str(exc)
    run_error = _run_state_error(snapshot[12])
    if run_error:
        return run_error
    paired = snapshot[13]
    if paired not in (0, 1):
        return "针配对状态必须为 0 或 1"
    needle_model = snapshot[14]
    remaining_uses = snapshot[15]
    remaining_time = _u16(snapshot, 16)
    needle_state = snapshot[18]
    if remaining_uses > 5:
        return "针剩余次数越界"
    if remaining_time > 1800:
        return "针剩余时间越界"
    if needle_state & 0xF8:
        return "针状态保留位非 0"
    if paired == 0:
        if snapshot[10:12] != b"\x00\x00":
            return "未配对时杆温必须填 0"
        if needle_model or remaining_uses or remaining_time or needle_state:
            return "未配对时针来源字段必须清零"
    return None


def semantic_error(frame: bytes) -> Optional[str]:
    """Validate fields of a structurally valid frame received by the PC."""
    command = frame[1]
    if command == 0xA0:
        return None if frame[2] in (0x00, 0x83, 0x84) else "0xA0 结果码非法"
    if command == 0xA1:
        allowed = set(range(0x00, 0x08)) | {
            0x80,
            0x81,
            0x82,
            0x83,
            0x85,
            0x87,
            0x88,
            0x89,
            0x8A,
            0x8B,
            0x8C,
        }
        return None if frame[2] in allowed else "0xA1 结果码非法"
    if command == 0xA2:
        result = frame[2]
        if result not in (0x00, 0x80, 0x81, 0x86):
            return "0xA2 结果码非法"
        if result != 0 and frame[3:8] != bytes(5):
            return "停止失败时针字段必须全部填 0"
        if result == 0:
            if frame[4] > 5 or _u16(frame, 5) > 1800:
                return "停止应答的针额度越界"
            if frame[7] & 0xF8:
                return "停止应答的针状态保留位非 0"
        return None
    if command == 0xA3:
        return None if frame[2] in (0x00, 0x83, 0x84) else "0xA3 结果码非法"
    if command == 0xA4:
        return _snapshot_error(frame[2:22])
    if command == 0x26:
        return _snapshot_error(frame[2:22])
    if command == 0x25:
        if frame[2] & 0xF8:
            return "实时数据的针状态保留位非 0"
        if frame[17] > 100:
            return "实时数据的输出功率越界"
        run_error = _run_state_error(frame[20])
        if run_error:
            return run_error
        if not frame[20] & 0x01:
            return "实时数据只允许在微波正式输出期间上报"
        return None
    return "PC 收到不应由 HW100 发送的命令 0x%02X" % command


def decode_snapshot(frame: bytes) -> Dict[str, object]:
    if frame[1] not in (0xA4, 0x26):
        raise ValueError("不是状态快照帧")
    error = structural_error(frame, PC_INBOUND_LENGTHS) or semantic_error(frame)
    if error:
        raise ValueError(error)
    data = frame[2:22]
    control = ControlParameters.from_bytes(data[:8])
    run_state = data[12]
    return {
        "control": control,
        "bypass_temp_c": _u16(data, 8) / 10.0,
        "rod_temp_c": (_u16(data, 10) / 10.0) if _u16(data, 10) else None,
        "rod_temp_available": bool(_u16(data, 10)),
        "run_state": run_state,
        "microwave_on": bool(run_state & 0x01),
        "pump_on": bool(run_state & 0x02),
        "start_pending": bool(run_state & 0x04),
        "settlement_pending": bool(run_state & 0x08),
        "settlement_unconfirmed": bool(run_state & 0x10),
        "needle_paired": bool(data[13]),
        "needle_model": data[14],
        "needle_remaining_uses": data[15],
        "needle_remaining_time_s": _u16(data, 16),
        "needle_state": data[18],
        "alarm_flag": data[19],
        "notification_sequence": frame[22] if frame[1] == 0x26 else None,
    }


def decode_realtime(frame: bytes) -> Dict[str, object]:
    if frame[1] != 0x25:
        raise ValueError("不是实时数据帧")
    error = structural_error(frame, PC_INBOUND_LENGTHS) or semantic_error(frame)
    if error:
        raise ValueError(error)
    return {
        "needle_state": frame[2],
        "rod_temp_c": _u16(frame, 3) / 10.0,
        "pitch_deg": _i16(frame, 5) / 10.0,
        "roll_deg": _i16(frame, 7) / 10.0,
        "yaw_deg": _i16(frame, 9) / 10.0,
        "mag_x": _i16(frame, 11),
        "mag_y": _i16(frame, 13),
        "mag_z": _i16(frame, 15),
        "power_w": frame[17],
        "bypass_temp_c": _u16(frame, 18) / 10.0,
        "run_state": frame[20],
        "alarm_flag": frame[21],
    }


def decode_stop_response(frame: bytes) -> Dict[str, int]:
    if frame[1] != 0xA2:
        raise ValueError("不是停止应答帧")
    error = structural_error(frame, PC_INBOUND_LENGTHS) or semantic_error(frame)
    if error:
        raise ValueError(error)
    return {
        "result": frame[2],
        "needle_model": frame[3],
        "needle_remaining_uses": frame[4],
        "needle_remaining_time_s": _u16(frame, 5),
        "needle_state": frame[7],
    }


def result_text(result: int) -> str:
    result = int(result) & 0xFF
    return RESULT_TEXT.get(result, "未知结果码 0x%02X" % result)


def alarm_text(flag: int) -> str:
    flag = int(flag) & 0xFF
    if flag == 0:
        return "消融仪状态正常"
    return "；".join(text for bit, text in ALARM_BITS if flag & bit)


class FrameStreamParser:
    """Incremental parser with fixed-length lookup and 20 ms fragment expiry."""

    def __init__(
        self,
        lengths: Optional[Dict[int, int]] = None,
        inter_byte_timeout_s: float = INTER_BYTE_TIMEOUT_S,
    ):
        self.lengths = dict(PC_INBOUND_LENGTHS if lengths is None else lengths)
        self.inter_byte_timeout_s = float(inter_byte_timeout_s)
        self.buffer = bytearray()
        self.last_byte_time: Optional[float] = None

    def reset(self) -> None:
        self.buffer.clear()
        self.last_byte_time = None

    def feed(self, data: bytes, now: float) -> Tuple[List[bytes], List[str]]:
        frames: List[bytes] = []
        issues: List[str] = []
        if self.buffer and self.last_byte_time is not None:
            if float(now) - self.last_byte_time > self.inter_byte_timeout_s:
                issues.append("残帧相邻字节超时")
                self._discard_candidate()
                old_frames, old_issues = self._consume()
                frames.extend(old_frames)
                issues.extend(old_issues)
        if data:
            self.buffer.extend(data)
            self.last_byte_time = float(now)
        new_frames, new_issues = self._consume()
        frames.extend(new_frames)
        issues.extend(new_issues)
        if not self.buffer:
            self.last_byte_time = None
        return frames, issues

    def expire(self, now: float) -> Tuple[List[bytes], List[str]]:
        if not self.buffer or self.last_byte_time is None:
            return [], []
        if float(now) - self.last_byte_time <= self.inter_byte_timeout_s:
            return [], []
        self._discard_candidate()
        frames, issues = self._consume()
        issues.insert(0, "残帧相邻字节超时")
        if not self.buffer:
            self.last_byte_time = None
        return frames, issues

    def _discard_candidate(self) -> None:
        try:
            index = self.buffer.index(FRAME_HEAD)
        except ValueError:
            self.buffer.clear()
            return
        del self.buffer[: index + 1]

    def _consume(self) -> Tuple[List[bytes], List[str]]:
        frames: List[bytes] = []
        issues: List[str] = []
        while True:
            try:
                start = self.buffer.index(FRAME_HEAD)
            except ValueError:
                self.buffer.clear()
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 2:
                break
            command = self.buffer[1]
            expected = self.lengths.get(command)
            if expected is None:
                issues.append("未知命令字 0x%02X" % command)
                del self.buffer[0]
                continue
            if len(self.buffer) < expected:
                break
            candidate = bytes(self.buffer[:expected])
            error = structural_error(candidate, self.lengths)
            if error:
                issues.append(error)
                # The protocol requires discarding only this head candidate.
                del self.buffer[0]
                continue
            del self.buffer[:expected]
            frames.append(candidate)
        return frames, issues

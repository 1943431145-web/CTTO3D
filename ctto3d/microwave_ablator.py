"""HW100 microwave ablation controller using the PC <-> HW100 V1 protocol.

The PC talks only to HW100.  RTX/RF/TRX transaction details stay behind that
boundary; their globally unique result codes and needle state are interpreted
when HW100 forwards them in A1/A2/A4/25/26 frames.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from PySide6 import QtCore, QtSerialPort

from . import hw100_protocol as protocol


log = logging.getLogger(__name__)

DEFAULT_BAUD = 115200
REQUEST_CHECK_MS = 50
LINK_CHECK_MS = 200
REALTIME_TIMEOUT_S = 2.5
MAX_WORK_TIME_S = 1800
MAX_POWER_W = 100


def status_text_from_flag(flag: int) -> str:
    """HW100 alarm byte (A4/25/26) to an operator-facing message."""
    return protocol.alarm_text(flag)


def format_work_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return "%02d:%02d" % (seconds // 60, seconds % 60)


def _mode_text(mode: int) -> str:
    return "连续" if int(mode) == 0 else "脉冲"


class MicrowaveAblationDevice(QtCore.QObject):
    """QtSerialPort-backed PC endpoint for the HW100 V1 protocol."""

    connectionChanged = QtCore.Signal(bool, str)
    statusChanged = QtCore.Signal(str)
    telemetryUpdated = QtCore.Signal(dict)
    logMessage = QtCore.Signal(str)
    errorOccurred = QtCore.Signal(str)
    portStatusChanged = QtCore.Signal(str, bool)
    bytesSent = QtCore.Signal(bytes)
    bytesReceived = QtCore.Signal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._serial = QtSerialPort.QSerialPort(self)
        self._serial.readyRead.connect(self._on_ready_read)
        self._serial.errorOccurred.connect(self._on_serial_error)

        self._parser = protocol.FrameStreamParser()
        self._pending_requests: Dict[int, dict] = {}
        self._queued_parameters: Optional[protocol.ControlParameters] = None

        self._online = False
        self._control: Optional[protocol.ControlParameters] = None
        self._cooling_on = False  # UI compatibility: this is the peristaltic pump
        self._microwave_on = False
        self._start_pending = False
        self._settlement_pending = False
        self._settlement_unconfirmed = False
        self._alarm_flag = 0
        self._needle_state = 0
        self._needle_paired = False
        self._needle_model = 0
        self._needle_remaining_uses = 0
        self._needle_remaining_time_s = 0
        self._last_rod_temp_c: Optional[float] = None
        self._last_bypass_temp_c: Optional[float] = None

        self._countdown_active = False
        self._countdown_remaining = 0
        self._countdown_expired_waiting_stop = False
        self._last_realtime_ts = 0.0
        self._realtime_interrupted = False

        self._fragment_timer = QtCore.QTimer(self)
        self._fragment_timer.setSingleShot(True)
        self._fragment_timer.setInterval(20)
        self._fragment_timer.timeout.connect(self._expire_fragment)

        self._request_timer = QtCore.QTimer(self)
        self._request_timer.setInterval(REQUEST_CHECK_MS)
        self._request_timer.timeout.connect(self._check_request_timeouts)

        self._link_timer = QtCore.QTimer(self)
        self._link_timer.setInterval(LINK_CHECK_MS)
        self._link_timer.timeout.connect(self._check_realtime_freshness)

        self._countdown_timer = QtCore.QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)

    # ---- port discovery / public state ---------------------------------

    @staticmethod
    def available_ports():
        ports = []
        for info in QtSerialPort.QSerialPortInfo.availablePorts():
            name = info.portName()
            details = " · ".join(
                value
                for value in (info.description(), info.manufacturer())
                if value
            )
            ports.append(
                {"name": name, "label": name if not details else "%s — %s" % (name, details)}
            )
        return ports

    def is_port_open(self):
        return self._serial.isOpen()

    def is_online(self):
        return self._online

    def is_cooling_on(self):
        return self._cooling_on

    def is_microwave_on(self):
        return self._microwave_on

    def is_start_pending(self):
        return self._start_pending

    def is_settlement_pending(self):
        return self._settlement_pending

    def is_countdown_active(self):
        return self._countdown_active

    def work_time_s(self):
        if self._countdown_active or self._countdown_expired_waiting_stop:
            return self._countdown_remaining
        return self._control.work_time_s if self._control is not None else 0

    def power_w(self):
        return self._control.power_w if self._control is not None else 0

    def connect_port(self, port_name, baud_rate=DEFAULT_BAUD):
        if self._serial.isOpen():
            self.disconnect_port()
        self._reset_protocol_state()
        self._serial.setPortName(str(port_name))
        self._serial.setBaudRate(int(baud_rate))
        self._serial.setDataBits(QtSerialPort.QSerialPort.DataBits.Data8)
        self._serial.setParity(QtSerialPort.QSerialPort.Parity.NoParity)
        self._serial.setStopBits(QtSerialPort.QSerialPort.StopBits.OneStop)
        self._serial.setFlowControl(QtSerialPort.QSerialPort.FlowControl.NoFlowControl)
        if not self._serial.open(QtCore.QIODevice.OpenModeFlag.ReadWrite):
            message = "微波消融仪串口连接失败：%s" % self._serial.errorString()
            self.portStatusChanged.emit(message, False)
            self.errorOccurred.emit(message)
            return False

        self.portStatusChanged.emit(
            "已连接 %s @ %s，正在读取 HW100 状态" % (port_name, baud_rate), True
        )
        self.logMessage.emit("打开串口 %s，协议 PC↔HW100 V1" % port_name)
        self._request_timer.start()
        self._link_timer.start()
        # 0x24 is sent once on connection.  Steady-state synchronization uses
        # acknowledged 0x26 notifications; the protocol explicitly forbids
        # periodic polling.
        return self.request_status()

    def disconnect_port(self):
        self._fragment_timer.stop()
        self._request_timer.stop()
        self._link_timer.stop()
        self._countdown_timer.stop()
        was_open = self._serial.isOpen()
        name = self._serial.portName() if was_open else ""
        if was_open:
            self._serial.close()
        was_online = self._online
        self._reset_protocol_state()
        if was_online:
            self.connectionChanged.emit(False, "微波消融仪主机已断开连接")
        self.statusChanged.emit("微波消融仪主机已断开连接")
        if was_open:
            self.portStatusChanged.emit("已断开 %s" % name, False)
        else:
            self.portStatusChanged.emit("微波消融仪串口未连接。", False)

    def close(self):
        self.disconnect_port()

    # ---- PC requests ----------------------------------------------------

    def request_status(self):
        if 0xA4 in self._pending_requests:
            return True
        return self._send_request(
            protocol.build_status_query(),
            response_command=0xA4,
            timeout_s=0.5,
            retry_count=1,
            label="状态查询",
        )

    def send_debug_frame(self, frame: bytes):
        """Send one complete PC -> HW100 frame entered in the HEX monitor."""
        frame = bytes(frame)
        error = protocol.structural_error(frame, protocol.PC_OUTBOUND_LENGTHS)
        if error:
            self.errorOccurred.emit("HEX 协议帧无效：%s" % error)
            return False
        return self._write_bytes(frame, "手动 HEX")

    def set_power(self, watts):
        base = self._parameter_edit_base()
        if base is None:
            return False
        desired = base.changed(power_w=max(0, min(MAX_POWER_W, int(watts))))
        return self._set_parameters(desired, "设置功率 %d W" % desired.power_w)

    def adjust_power(self, delta):
        base = self._parameter_edit_base()
        if base is None:
            return False
        return self.set_power(base.power_w + int(delta))

    def set_work_time(self, seconds):
        base = self._parameter_edit_base()
        if base is None:
            return False
        desired = base.changed(
            work_time_s=max(0, min(MAX_WORK_TIME_S, int(seconds)))
        )
        return self._set_parameters(
            desired, "设置时间 %s" % format_work_time(desired.work_time_s)
        )

    def adjust_work_time(self, delta_seconds):
        base = self._parameter_edit_base()
        if base is None:
            return False
        return self.set_work_time(base.work_time_s + int(delta_seconds))

    def set_output_mode(self, mode):
        base = self._parameter_edit_base()
        if base is None:
            return False
        desired = base.changed(mode=int(mode))
        return self._set_parameters(desired, "设置输出模式：%s" % _mode_text(mode))

    def set_alarm_thresholds(self, bypass_c, rod_c):
        base = self._parameter_edit_base()
        if base is None:
            return False
        desired = base.changed(
            bypass_alarm_deci_c=int(round(float(bypass_c) * 10.0)),
            rod_alarm_deci_c=int(round(float(rod_c) * 10.0)),
        )
        return self._set_parameters(desired, "设置温度报警阈值")

    def _parameter_edit_base(self):
        if self._queued_parameters is not None:
            return self._queued_parameters
        pending = self._pending_requests.get(0xA0)
        if pending is not None and pending.get("context") is not None:
            return pending["context"]
        if self._control is None:
            self.errorOccurred.emit("尚未取得 HW100 控制参数，请等待状态查询应答。")
            self.request_status()
            return None
        return self._control

    def _set_parameters(self, desired, label):
        error = desired.validation_error()
        if error:
            self.errorOccurred.emit(error)
            return False
        # Rapid +/- clicks are coalesced into one latest full block.  The next
        # 0x20 is not transmitted until the current A0 arrives, preserving the
        # protocol's one-outstanding-control-request rule.
        if 0xA0 in self._pending_requests:
            self._queued_parameters = desired
            self.logMessage.emit("参数请求处理中，已合并下一次参数调整")
            return True
        ok = self._send_request(
            protocol.build_parameter_set(desired),
            response_command=0xA0,
            timeout_s=0.5,
            retry_count=1,
            label=label,
            context=desired,
        )
        if ok:
            self.logMessage.emit(label)
        return ok

    def set_cooling(self, enabled):
        enabled = bool(enabled)
        return self._send_request(
            protocol.build_pump_control(enabled),
            response_command=0xA3,
            timeout_s=0.5,
            retry_count=1,
            label="蠕动泵%s" % ("启动" if enabled else "停止"),
            context=enabled,
        )

    def set_microwave(self, enabled):
        enabled = bool(enabled)
        if enabled:
            if self._control is None:
                self.errorOccurred.emit("尚未取得 HW100 状态，不能启动微波。")
                self.request_status()
                return False
            if self._control.power_w <= 0 or self._control.work_time_s <= 0:
                self.errorOccurred.emit("请先设置非零功率和工作时间。")
                return False
            if not self._cooling_on:
                self.errorOccurred.emit("请先启动蠕动泵。")
                return False
            # At the first local gate rod-temperature bit1 is intentionally
            # excluded: idle has no fresh rod temperature.  HW100 obtains a new
            # pre-start sample before approving the charged session.
            if self._alarm_flag & 0xFD:
                self.errorOccurred.emit(
                    "存在阻断性告警：%s" % protocol.alarm_text(self._alarm_flag)
                )
                return False
            ok = self._send_request(
                protocol.build_start(),
                response_command=0xA1,
                timeout_s=12.0,
                retry_count=0,  # V1 start has no request id and is not idempotent.
                label="启动微波",
            )
            if ok:
                self._start_pending = True
                self.statusChanged.emit("启动处理中，等待 HW100 完成安全联锁与针端提交")
                self._emit_state_payload()
            return ok

        # Stop is legal in every state and may preempt an outstanding A1.
        ok = self._send_request(
            protocol.build_stop(),
            response_command=0xA2,
            timeout_s=10.0,
            retry_count=1,
            label="停止微波并结算",
        )
        if ok:
            self.statusChanged.emit("停止指令已发送，等待针端结算结果")
        return ok

    def toggle_cooling(self):
        return self.set_cooling(not self._cooling_on)

    def toggle_microwave(self):
        if self._microwave_on or self._start_pending:
            return self.set_microwave(False)
        return self.set_microwave(True)

    def _send_request(
        self,
        frame: bytes,
        response_command: int,
        timeout_s: float,
        retry_count: int,
        label: str,
        context=None,
    ):
        if not self._serial.isOpen():
            self.errorOccurred.emit("微波消融仪串口未连接，无法发送。")
            return False
        if response_command in self._pending_requests:
            self.errorOccurred.emit("%s请求仍在处理中，请勿重复发送。" % label)
            return False

        is_control = response_command in (0xA0, 0xA1, 0xA2, 0xA3)
        if is_control:
            active = set(self._pending_requests).intersection({0xA0, 0xA1, 0xA2, 0xA3})
            stop_preempts_start = response_command == 0xA2 and active == {0xA1}
            if active and not stop_preempts_start:
                self.errorOccurred.emit("已有控制请求正在处理中，请等待应答。")
                return False

        if not self._write_bytes(frame, label):
            return False
        self._pending_requests[response_command] = {
            "frame": frame,
            "deadline": time.monotonic() + float(timeout_s),
            "timeout_s": float(timeout_s),
            "retries_left": int(retry_count),
            "label": label,
            "context": context,
        }
        return True

    # ---- serial stream --------------------------------------------------

    def _write_bytes(self, data: bytes, label=""):
        if not self._serial.isOpen():
            self.errorOccurred.emit("微波消融仪串口未连接，无法发送。")
            return False
        written = self._serial.write(data)
        if written < 0:
            self.errorOccurred.emit("串口发送失败：%s" % self._serial.errorString())
            return False
        if written != len(data):
            self.errorOccurred.emit(
                "串口仅接收 %d/%d 字节，帧未完整入队。" % (written, len(data))
            )
            return False
        self._serial.flush()
        self.bytesSent.emit(bytes(data))
        suffix = " · %s" % label if label else ""
        self.logMessage.emit("TX %s%s" % (data.hex(" ").upper(), suffix))
        return True

    def _on_ready_read(self):
        raw = bytes(self._serial.readAll())
        if not raw:
            return
        self.bytesReceived.emit(raw)
        frames, issues = self._parser.feed(raw, time.monotonic())
        self._record_protocol_issues(issues)
        self._handle_frames(frames)
        if self._parser.buffer:
            self._fragment_timer.start(20)
        else:
            self._fragment_timer.stop()

    def _expire_fragment(self):
        frames, issues = self._parser.expire(time.monotonic())
        self._record_protocol_issues(issues)
        self._handle_frames(frames)
        # A timer may fire a fraction early on some Windows timer resolutions.
        if self._parser.buffer:
            self._fragment_timer.start(5)

    def _record_protocol_issues(self, issues):
        for issue in issues:
            log.warning("HW100 协议丢帧：%s", issue)
            self.logMessage.emit("RX 丢弃：%s" % issue)

    def _handle_frames(self, frames):
        for frame in frames:
            error = protocol.semantic_error(frame)
            if error:
                # In particular an invalid 0x26 receives no A6, so HW100 will
                # retry it according to the protocol.
                self._record_protocol_issues([error])
                continue
            self.logMessage.emit("RX %s" % frame.hex(" ").upper())
            self._set_online()
            command = frame[1]
            if command in (0xA0, 0xA1, 0xA3):
                self._handle_simple_response(frame)
            elif command == 0xA2:
                self._handle_stop_response(frame)
            elif command == 0xA4:
                self._pending_requests.pop(0xA4, None)
                self._apply_snapshot(protocol.decode_snapshot(frame))
            elif command == 0x25:
                self._handle_realtime(protocol.decode_realtime(frame))
            elif command == 0x26:
                snapshot = protocol.decode_snapshot(frame)
                self._apply_snapshot(snapshot)
                # Apply/emit first, then acknowledge exactly the sequence from
                # this frame.  Duplicate notifications are intentionally
                # applied and acknowledged again because snapshots are idempotent.
                ack = protocol.build_notification_ack(snapshot["notification_sequence"])
                self._write_bytes(ack, "状态变更确认")

    # ---- responses and decoded state -----------------------------------

    def _handle_simple_response(self, frame):
        command = frame[1]
        result = frame[2]
        request = self._pending_requests.pop(command, None)
        label = request.get("label", "命令") if request else "未归属应答"
        message = "%s：%s" % (label, protocol.result_text(result))
        self.logMessage.emit(message)

        if command == 0xA0:
            if result == 0x00 and request and request.get("context") is not None:
                self._control = request["context"]
                self._emit_state_payload(status_text="参数设置成功")
                queued = self._queued_parameters
                self._queued_parameters = None
                if queued is not None and queued != self._control:
                    QtCore.QTimer.singleShot(
                        0, lambda value=queued: self._set_parameters(value, "合并后的参数设置")
                    )
            else:
                self._queued_parameters = None
        elif command == 0xA3:
            if result == 0x00 and request:
                self._cooling_on = bool(request.get("context"))
                self._emit_state_payload(status_text=message)
        elif command == 0xA1:
            self._start_pending = False
            if result == 0x00:
                self._microwave_on = True
                self._settlement_pending = False
                self._last_realtime_ts = time.monotonic()
                self._start_countdown(self._configured_work_time())
                self._emit_state_payload(status_text="启动成功，微波已输出")
            else:
                self._microwave_on = False
                if result in (0x88, 0x8A, 0x8B, 0x8C):
                    self._settlement_pending = True
                elif result == 0x85 and 0xA2 in self._pending_requests:
                    self._settlement_pending = True
                self._stop_countdown()
                self._emit_state_payload(status_text=message)

        if result != 0x00:
            self.errorOccurred.emit(message)
        else:
            self.statusChanged.emit(message)

    def _handle_stop_response(self, frame):
        self._pending_requests.pop(0xA2, None)
        data = protocol.decode_stop_response(frame)
        result = data["result"]
        self._microwave_on = False
        self._start_pending = False
        self._stop_countdown()
        if result == 0x00:
            self._settlement_pending = False
            self._needle_model = data["needle_model"]
            self._needle_remaining_uses = data["needle_remaining_uses"]
            self._needle_remaining_time_s = data["needle_remaining_time_s"]
            self._needle_state = data["needle_state"]
            if self._needle_state & 0x04:
                message = "微波已停；针存储异常，结算值未确认持久化"
            else:
                message = "微波已停，针端结算完成"
        elif result in (0x80, 0x81):
            self._settlement_pending = True
            message = "微波已停；%s，后台结算仍在继续" % protocol.result_text(result)
        else:  # 0x86
            self._settlement_pending = False
            self._settlement_unconfirmed = True
            message = "微波已停；上一次消融的结算未能确认（针已更换）"
        self._emit_state_payload(status_text=message, stop_result=result)
        if result == 0x00 and not (self._needle_state & 0x04):
            self.statusChanged.emit(message)
        else:
            self.errorOccurred.emit(message)

    def _apply_snapshot(self, snapshot):
        was_microwave_on = self._microwave_on
        self._control = snapshot["control"]
        self._cooling_on = snapshot["pump_on"]
        self._microwave_on = snapshot["microwave_on"]
        self._start_pending = snapshot["start_pending"]
        self._settlement_pending = snapshot["settlement_pending"]
        self._settlement_unconfirmed = snapshot["settlement_unconfirmed"]
        self._needle_paired = snapshot["needle_paired"]
        self._needle_model = snapshot["needle_model"]
        self._needle_remaining_uses = snapshot["needle_remaining_uses"]
        self._needle_remaining_time_s = snapshot["needle_remaining_time_s"]
        self._needle_state = snapshot["needle_state"]
        self._alarm_flag = snapshot["alarm_flag"]
        self._last_bypass_temp_c = snapshot["bypass_temp_c"]
        self._last_rod_temp_c = snapshot["rod_temp_c"]

        if self._microwave_on:
            if not was_microwave_on and not self._countdown_expired_waiting_stop:
                self._start_countdown(self._configured_work_time())
            if self._last_realtime_ts <= 0:
                self._last_realtime_ts = time.monotonic()
        else:
            self._stop_countdown()

        message = self._current_status_text()
        self.telemetryUpdated.emit(
            self._state_payload(
                bypass_temp_c=snapshot["bypass_temp_c"],
                rod_temp_c=snapshot["rod_temp_c"],
                rod_temp_available=snapshot["rod_temp_available"],
                status_text=message,
                snapshot=True,
            )
        )
        self.statusChanged.emit(message)

    def _handle_realtime(self, data):
        self._last_realtime_ts = time.monotonic()
        recovered = self._realtime_interrupted
        self._realtime_interrupted = False
        self._needle_state = data["needle_state"]
        self._last_rod_temp_c = data["rod_temp_c"]
        self._last_bypass_temp_c = data["bypass_temp_c"]
        self._alarm_flag = data["alarm_flag"]
        self._cooling_on = bool(data["run_state"] & 0x02)
        self._microwave_on = True
        if not self._countdown_active and not self._countdown_expired_waiting_stop:
            self._start_countdown(self._configured_work_time())
        message = self._current_status_text()
        payload = self._state_payload(
            bypass_temp_c=data["bypass_temp_c"],
            rod_temp_c=data["rod_temp_c"],
            rod_temp_available=True,
            power_w=data["power_w"],
            status_text=message,
            realtime=True,
        )
        payload.update(
            {
                "pitch_deg": data["pitch_deg"],
                "roll_deg": data["roll_deg"],
                "yaw_deg": data["yaw_deg"],
                "mag_x": data["mag_x"],
                "mag_y": data["mag_y"],
                "mag_z": data["mag_z"],
                "magnetic_available": any(
                    data[key] != 0 for key in ("mag_x", "mag_y", "mag_z")
                ),
            }
        )
        self.telemetryUpdated.emit(payload)
        if recovered:
            self.statusChanged.emit("实时数据已恢复")

    def _configured_work_time(self):
        return self._control.work_time_s if self._control is not None else 0

    def _current_status_text(self):
        if self._settlement_unconfirmed:
            return "上一次消融的结算未能确认（针已更换）"
        if self._alarm_flag:
            return protocol.alarm_text(self._alarm_flag)
        if self._settlement_pending:
            return "停止／针端结算处理中"
        if self._start_pending:
            return "启动处理中"
        if self._microwave_on:
            return "微波输出中"
        return "消融仪状态正常"

    def _state_payload(self, **overrides):
        control = self._control
        time_s = control.work_time_s if control is not None else 0
        power_w = control.power_w if control is not None else 0
        mode = _mode_text(control.mode) if control is not None else None
        display_time = (
            self._countdown_remaining
            if self._countdown_active or self._countdown_expired_waiting_stop
            else time_s
        )
        payload = {
            "power_w": power_w,
            "time_s": time_s,
            "display_time_s": display_time,
            "elapsed_time_s": max(0, time_s - display_time),
            "side_alarm_c": (
                control.bypass_alarm_deci_c / 10.0 if control is not None else None
            ),
            "rod_alarm_c": (
                control.rod_alarm_deci_c / 10.0 if control is not None else None
            ),
            "bypass_temp_c": self._last_bypass_temp_c,
            "rod_temp_c": self._last_rod_temp_c,
            "rod_temp_available": self._last_rod_temp_c is not None,
            "mode": mode,
            "cooling_on": self._cooling_on,
            "pump_on": self._cooling_on,
            "microwave_on": self._microwave_on,
            "start_pending": self._start_pending,
            "settlement_pending": self._settlement_pending,
            "settlement_unconfirmed": self._settlement_unconfirmed,
            "countdown_active": self._countdown_active,
            "status_flag": self._alarm_flag,
            "status_text": self._current_status_text(),
            "needle_paired": self._needle_paired,
            "needle_model": self._needle_model,
            "needle_remaining_uses": self._needle_remaining_uses,
            "needle_remaining_time_s": self._needle_remaining_time_s,
            "needle_state": self._needle_state,
            "online": self._online,
        }
        payload.update(overrides)
        return payload

    def _emit_state_payload(self, **overrides):
        self.telemetryUpdated.emit(self._state_payload(**overrides))

    # ---- timers / state -------------------------------------------------

    def _set_online(self):
        if self._online:
            return
        self._online = True
        message = "微波消融仪主机已连接（PC↔HW100 V1）"
        self.connectionChanged.emit(True, message)
        self.statusChanged.emit(message)

    def _reset_protocol_state(self):
        self._parser.reset()
        self._pending_requests.clear()
        self._queued_parameters = None
        self._online = False
        self._control = None
        self._cooling_on = False
        self._microwave_on = False
        self._start_pending = False
        self._settlement_pending = False
        self._settlement_unconfirmed = False
        self._alarm_flag = 0
        self._needle_state = 0
        self._needle_paired = False
        self._needle_model = 0
        self._needle_remaining_uses = 0
        self._needle_remaining_time_s = 0
        self._last_rod_temp_c = None
        self._last_bypass_temp_c = None
        self._last_realtime_ts = 0.0
        self._realtime_interrupted = False
        self._stop_countdown()

    def _check_request_timeouts(self):
        now = time.monotonic()
        expired = [
            command
            for command, request in self._pending_requests.items()
            if now >= request["deadline"]
        ]
        for command in expired:
            request = self._pending_requests.get(command)
            if request is None:
                continue
            if request["retries_left"] > 0:
                if self._write_bytes(request["frame"], request["label"] + "重试"):
                    request["retries_left"] -= 1
                    request["deadline"] = now + request["timeout_s"]
                    continue
            self._pending_requests.pop(command, None)
            label = request["label"]
            if command == 0xA1:
                message = (
                    "启动应答超时；V1 启动不可自动重发，正在查询主机状态以重新对齐"
                )
                self._start_pending = True  # remains unknown until A4/26 resolves it
                self.errorOccurred.emit(message)
                self.request_status()
            else:
                message = "%s应答超时" % label
                if command == 0xA0:
                    self._queued_parameters = None
                if command == 0xA4 and not self._online:
                    self.connectionChanged.emit(False, "HW100 状态查询无应答")
                self.errorOccurred.emit(message)

    def _check_realtime_freshness(self):
        if not self._microwave_on or self._last_realtime_ts <= 0:
            return
        if time.monotonic() - self._last_realtime_ts <= REALTIME_TIMEOUT_S:
            return
        if self._realtime_interrupted:
            return
        self._realtime_interrupted = True
        message = "实时数据中断（超过 2.5s 未收到 0x25）"
        self.errorOccurred.emit(message)
        self._emit_state_payload(status_text=message, realtime_interrupted=True)

    def _start_countdown(self, seconds):
        self._countdown_remaining = max(0, int(seconds))
        self._countdown_active = self._countdown_remaining > 0
        self._countdown_expired_waiting_stop = False
        if self._countdown_active:
            self._countdown_timer.start()

    def _stop_countdown(self):
        self._countdown_timer.stop()
        self._countdown_active = False
        self._countdown_remaining = 0
        self._countdown_expired_waiting_stop = False

    def _on_countdown_tick(self):
        if not self._countdown_active:
            return
        self._countdown_remaining = max(0, self._countdown_remaining - 1)
        self._emit_state_payload(countdown_tick=True)
        if self._countdown_remaining <= 0:
            # This timer is display-only.  HW100 owns the authoritative work
            # timer and must initiate its own stop/settlement; sending 0x22 here
            # would turn harmless PC clock drift into an early stop.
            self._countdown_timer.stop()
            self._countdown_active = False
            self._countdown_expired_waiting_stop = True
            self.statusChanged.emit("本地显示倒计时结束，等待 HW100 停止与结算通知")

    def _on_serial_error(self, error):
        if error == QtSerialPort.QSerialPort.SerialPortError.NoError:
            return
        message = self._serial.errorString()
        if message:
            self.errorOccurred.emit(message)
        if error == QtSerialPort.QSerialPort.SerialPortError.ResourceError:
            self.disconnect_port()

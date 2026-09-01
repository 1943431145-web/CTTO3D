"""网络分析仪（VNA）TCP/SCPI 连接管理。

参考 E:\\PyQt_Work\\network_analyzer_manager.py，改为 PySide6 信号，
默认连接 SCPI 端口 5025，连接后发送 *IDN? 做连通性校验。
"""

import socket
import threading
import time

from PySide6 import QtCore


DEFAULT_PORT = 5025
DEFAULT_TIMEOUT_S = 5


class NetworkAnalyzerManager(QtCore.QObject):
    """网络分析仪管理器：TCP Socket + SCPI 指令。"""

    connection_status_changed = QtCore.Signal(bool)
    data_received = QtCore.Signal(str)
    error_occurred = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = None
        self.connected = False
        self.ip_address = ""
        self.port = DEFAULT_PORT
        self.timeout = DEFAULT_TIMEOUT_S
        self.last_idn = ""
        self._io_lock = threading.RLock()

    def connect(self, ip_address, port=None):
        """连接网络分析仪。成功返回 True。"""
        try:
            if self.connected:
                self.disconnect_analyzer()

            ip_address = (ip_address or "").strip()
            if not ip_address:
                self.error_occurred.emit("请输入有效的 IP 地址")
                return False

            try:
                socket.inet_aton(ip_address)
            except OSError:
                self.error_occurred.emit("IP 地址格式无效 — %s" % ip_address)
                return False

            if port is None:
                port = self.port
            try:
                port = int(port)
            except (TypeError, ValueError):
                self.error_occurred.emit("端口号无效")
                return False
            if not (1 <= port <= 65535):
                self.error_occurred.emit("端口号超出范围 (1–65535)")
                return False

            self.ip_address = ip_address
            self.port = port
            self.last_idn = ""

            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.ip_address, self.port))

            device_info = self.send_command("*IDN?", skip_connection_check=True)
            if device_info:
                self.connected = True
                self.last_idn = device_info
                self.connection_status_changed.emit(True)
                return True

            self.error_occurred.emit("连接测试失败，无法获取设备信息")
            self.cleanup()
            return False

        except socket.timeout:
            self.error_occurred.emit("连接超时，请检查网络和设备状态")
            self.cleanup()
            return False
        except ConnectionRefusedError:
            self.error_occurred.emit("连接被拒绝，请检查设备是否开启")
            self.cleanup()
            return False
        except Exception as exc:
            self.error_occurred.emit("连接失败: %s" % exc)
            self.cleanup()
            return False

    def disconnect_analyzer(self):
        """断开网络分析仪连接。"""
        with self._io_lock:
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
            self.cleanup()
        self.connection_status_changed.emit(False)

    def cleanup(self):
        """清理连接资源（不发状态信号）。"""
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
        self.connected = False
        self.ip_address = ""
        self.last_idn = ""

    def send_command(
        self,
        command,
        timeout=None,
        skip_connection_check=False,
        skip_response=False,
    ):
        """发送 SCPI 命令并返回响应字符串；失败返回 None。"""
        if not skip_connection_check and (not self.connected or not self.socket):
            self.error_occurred.emit("未连接到网络分析仪")
            return None

        try:
            with self._io_lock:
                current_timeout = self.timeout if timeout is None else timeout
                self.socket.settimeout(current_timeout)

                if not command.endswith("\n"):
                    command += "\n"
                self.socket.send(command.encode())

                if skip_response:
                    time.sleep(0.01)
                    return "OK"

                chunks = []
                total = 0
                max_bytes = 2_000_000
                while True:
                    part = self.socket.recv(4096)
                    if not part:
                        break
                    chunks.append(part)
                    total += len(part)
                    if b"\n" in part:
                        break
                    if total >= max_bytes:
                        break

                return b"".join(chunks).decode(errors="ignore").strip()

        except socket.timeout:
            self.error_occurred.emit("命令超时: %s" % command.strip())
            return None
        except Exception as exc:
            self.error_occurred.emit("发送命令失败: %s" % exc)
            return None

    def is_connected(self):
        return self.connected

    def get_connection_info(self):
        if not self.connected:
            return None
        return {
            "ip_address": self.ip_address,
            "port": self.port,
            "connected": self.connected,
            "idn": self.last_idn,
        }

    def set_timeout(self, timeout):
        self.timeout = timeout
        if self.socket:
            self.socket.settimeout(timeout)

    def set_frequency_range(self, start_freq_mhz, stop_freq_mhz):
        """设置扫频起止频率（单位 MHz）。"""
        if not self.connected:
            self.error_occurred.emit("未连接到网络分析仪")
            return False
        try:
            start_freq = float(start_freq_mhz)
            stop_freq = float(stop_freq_mhz)
            if start_freq <= 0 or stop_freq <= 0:
                self.error_occurred.emit("频率值必须大于 0")
                return False
            if start_freq >= stop_freq:
                self.error_occurred.emit("起始频率必须小于终止频率")
                return False

            start_hz = int(start_freq * 1e6)
            stop_hz = int(stop_freq * 1e6)
            result = self.send_command(
                "SENSe1:FREQuency:STARt %d;STOP %d" % (start_hz, stop_hz),
                skip_response=True,
            )
            if result is None:
                self.error_occurred.emit("频率设置失败，请检查设备状态")
                return False
            self.data_received.emit(
                "频率范围设置成功: %sMHz - %sMHz" % (start_freq_mhz, stop_freq_mhz)
            )
            return True
        except ValueError:
            self.error_occurred.emit("请输入有效的数字频率值")
            return False
        except Exception as exc:
            self.error_occurred.emit("设置频率失败: %s" % exc)
            return False

    def set_sweep_points(self, points):
        """设置采集点数。"""
        if not self.connected:
            self.error_occurred.emit("未连接到网络分析仪")
            return False
        try:
            points_int = int(points)
            if points_int <= 0:
                self.error_occurred.emit("采集点数必须大于 0")
                return False
            if points_int > 10000:
                self.error_occurred.emit("采集点数不能超过 10000")
                return False
            result = self.send_command(
                "SENSe1:SWEep:POINts %d" % points_int,
                skip_response=True,
            )
            if result is None:
                self.error_occurred.emit("采集点数设置失败，请检查设备状态")
                return False
            self.data_received.emit("采集点数设置成功: %d" % points_int)
            return True
        except ValueError:
            self.error_occurred.emit("请输入有效的数字采集点数")
            return False
        except Exception as exc:
            self.error_occurred.emit("设置采集点数失败: %s" % exc)
            return False

    def check_marker_state(self, timeout=None):
        if not self.connected:
            self.error_occurred.emit("未连接到网络分析仪")
            return None
        try:
            response = self.send_command("CALCulate1:MARKer1:STATe?", timeout=timeout)
            if response is None:
                self.error_occurred.emit("查询 mark1 点状态失败")
                return None
            return response.strip() == "1"
        except Exception as exc:
            self.error_occurred.emit("查询 mark1 点状态失败: %s" % exc)
            return None

    def set_marker_frequency(self, frequency_mhz):
        """设置 mark1 频率（单位 MHz）。"""
        if not self.connected:
            self.error_occurred.emit("未连接到网络分析仪")
            return False
        try:
            frequency = float(frequency_mhz)
            if frequency <= 0:
                self.error_occurred.emit("频率值必须大于 0")
                return False

            marker_state = self.check_marker_state()
            if marker_state is None:
                return False
            if not marker_state:
                result = self.send_command("CALCULATE1:MARKER1 ON", skip_response=True)
                if result is None:
                    self.error_occurred.emit("开启 mark1 点失败")
                    return False
                self.data_received.emit("已开启 mark1 点")

            frequency_hz = int(frequency * 1e6)
            result = self.send_command(
                "CALCulate1:MARKer1:X %d" % frequency_hz,
                skip_response=True,
            )
            if result is None:
                self.error_occurred.emit("设置 mark1 点频率失败")
                return False
            self.data_received.emit("mark1 点频率设置成功: %sMHz" % frequency_mhz)
            return True
        except ValueError:
            self.error_occurred.emit("请输入有效的数字频率值")
            return False
        except Exception as exc:
            self.error_occurred.emit("设置 mark1 点频率失败: %s" % exc)
            return False

    def get_trace_fdata(self, trace_format):
        """获取 CALC1 Trace（FDATa），返回 list[float] 或 None。"""
        if not self.connected:
            self.error_occurred.emit("未连接到网络分析仪")
            return None

        fmt = (trace_format or "").strip().upper()
        if fmt not in ("MLOG", "MLIN", "PHAS"):
            self.error_occurred.emit("不支持的 Trace 格式: %s" % trace_format)
            return None

        try:
            result = self.send_command("CALCulate1:FORMat %s" % fmt, skip_response=True)
            if result is None:
                self.error_occurred.emit("设置 %s 格式失败" % fmt)
                return None

            resp = self.send_command("CALCulate1:DATA? FDATa")
            if not resp:
                self.error_occurred.emit("获取 FDATa 数据失败")
                return None

            values = []
            for part in resp.replace("\n", "").split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    values.append(float(part))
                except ValueError:
                    continue
            if not values:
                self.error_occurred.emit("FDATa 数据解析失败")
                return None
            return values
        except Exception as exc:
            self.error_occurred.emit("获取 Trace 数据失败: %s" % exc)
            return None

    def get_mlog_trace_fdata(self):
        return self.get_trace_fdata("MLOG")

    def get_marker_data(
        self,
        timeout=None,
        check_state=True,
        emit_messages=True,
        restore_format=True,
    ):
        """获取 mark1 的 MLOG / MLIN / PHAS，返回 dict 或 None。"""
        if not self.connected:
            self.error_occurred.emit("未连接到网络分析仪")
            return None

        try:
            if check_state:
                marker_state = self.check_marker_state(timeout=timeout)
                if marker_state is None:
                    return None
                if not marker_state:
                    self.error_occurred.emit("mark1 点未开启，请先开启 mark1 点")
                    return None

            marker_data = {}

            def _set_format(fmt):
                result = self.send_command(
                    "CALCulate1:MARKer1:FORMat %s" % fmt,
                    timeout=timeout,
                    skip_response=True,
                )
                return result is not None

            def _query_y():
                return self.send_command("CALCulate1:MARKer1:Y?", timeout=timeout)

            for key, fmt in (("mlog", "MLOG"), ("mlin", "MLIN"), ("phas", "PHAS")):
                if not _set_format(fmt):
                    self.error_occurred.emit("设置 %s 格式失败" % fmt)
                    return None
                value = _query_y()
                if value is None:
                    self.error_occurred.emit("获取 %s 数据失败" % fmt)
                    return None
                marker_data[key] = value.strip()
                if emit_messages:
                    self.data_received.emit("%s 数据: %s" % (fmt, marker_data[key]))

            if restore_format:
                self.send_command(
                    "CALCulate1:MARKer1:FORMat DEF",
                    timeout=timeout,
                    skip_response=True,
                )
            if emit_messages:
                self.data_received.emit("mark1 点数据获取完成")
            return marker_data
        except Exception as exc:
            self.error_occurred.emit("获取 mark1 点数据失败: %s" % exc)
            return None

"""Serial-port connection helper built on QtSerialPort.

The UI uses this small QObject wrapper so serial I/O stays inside Qt's event
loop instead of needing a worker thread. It is intentionally generic: the
caller can list ports, connect/disconnect, send text or binary frames, receive
exact byte chunks, and retain the existing decoded-text monitor.
"""

import re

from PySide6 import QtCore, QtSerialPort


BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]


def parse_hex_bytes(text):
    """Parse common serial-tool HEX notation into exact bytes.

    Accepted examples include ``AA 13 00 FF``, ``AA1300FF`` and
    ``0xAA,0x13,0x00,0xFF``.  Every byte must contain exactly two hex digits.
    """
    value = str(text).strip()
    if not value:
        raise ValueError("请输入十六进制字节。")
    value = re.sub(r"0[xX]", "", value)
    compact = re.sub(r"[\s,;:_-]+", "", value)
    if not compact or re.search(r"[^0-9A-Fa-f]", compact):
        raise ValueError("HEX 数据只能包含 0-9、A-F 和字节分隔符。")
    if len(compact) % 2:
        raise ValueError("HEX 数据必须按完整字节输入，每个字节为两位。")
    return bytes.fromhex(compact)


class SerialConnection(QtCore.QObject):
    statusChanged = QtCore.Signal(str, bool)
    dataReceived = QtCore.Signal(str)
    rawDataReceived = QtCore.Signal(bytes)
    errorOccurred = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._serial = QtSerialPort.QSerialPort(self)
        self._serial.readyRead.connect(self._on_ready_read)
        self._serial.errorOccurred.connect(self._on_error)
        # 逐行缓冲原始字节：下位机(RF)调试串是 GBK 编码，按行解码可避免
        # GBK 双字节被 readyRead 分片截断。
        self._rx_buffer = bytearray()

    def is_available(self):
        return True

    def is_connected(self):
        return self._serial.isOpen()

    def available_ports(self):
        ports = []
        for info in QtSerialPort.QSerialPortInfo.availablePorts():
            name = info.portName()
            description = info.description()
            manufacturer = info.manufacturer()
            details = " · ".join(part for part in (description, manufacturer) if part)
            label = name if not details else "%s — %s" % (name, details)
            ports.append({
                "name": name,
                "label": label,
                "description": description,
                "manufacturer": manufacturer,
                "serial_number": info.serialNumber(),
                "system_location": info.systemLocation(),
            })
        return ports

    def connect_port(self, port_name, baud_rate):
        if self._serial.isOpen():
            self._serial.close()
        self._serial.setPortName(port_name)
        self._serial.setBaudRate(int(baud_rate))
        self._serial.setDataBits(QtSerialPort.QSerialPort.DataBits.Data8)
        self._serial.setParity(QtSerialPort.QSerialPort.Parity.NoParity)
        self._serial.setStopBits(QtSerialPort.QSerialPort.StopBits.OneStop)
        self._serial.setFlowControl(QtSerialPort.QSerialPort.FlowControl.NoFlowControl)
        if not self._serial.open(QtCore.QIODevice.OpenModeFlag.ReadWrite):
            message = "串口连接失败：%s" % self._serial.errorString()
            self.statusChanged.emit(message, False)
            self.errorOccurred.emit(message)
            return False
        self._rx_buffer.clear()
        self.statusChanged.emit("已连接 %s @ %s" % (port_name, baud_rate), True)
        return True

    def disconnect_port(self):
        self._rx_buffer.clear()
        if self._serial.isOpen():
            name = self._serial.portName()
            self._serial.close()
            self.statusChanged.emit("已断开 %s" % name, False)
        else:
            self.statusChanged.emit("串口未连接。", False)

    def send_text(self, text, append_newline=True):
        if not self._serial.isOpen():
            self.errorOccurred.emit("串口未连接，无法发送。")
            return False
        payload = text
        if append_newline:
            payload += "\r\n"
        data = payload.encode("utf-8", errors="replace")
        written = self._serial.write(data)
        if written == -1:
            self.errorOccurred.emit("串口发送失败：%s" % self._serial.errorString())
            return False
        self._serial.flush()
        return True

    def send_bytes(self, data):
        """Send an already-framed binary protocol message unchanged."""
        if not self._serial.isOpen():
            self.errorOccurred.emit("串口未连接，无法发送。")
            return False
        payload = bytes(data)
        written = self._serial.write(payload)
        if written != len(payload):
            self.errorOccurred.emit("串口发送失败：%s" % self._serial.errorString())
            return False
        self._serial.flush()
        return True

    def close(self):
        if self._serial.isOpen():
            self._serial.close()

    def clear_text_receive_buffer(self):
        """Drop only the decoded-text staging buffer after a display-mode switch."""
        self._rx_buffer.clear()

    def _on_ready_read(self):
        raw = bytes(self._serial.readAll())
        if not raw:
            return
        # Binary protocol consumers must see the exact bytes before the
        # generic text monitor applies GBK decoding or line buffering.
        self.rawDataReceived.emit(raw)
        self._rx_buffer.extend(raw)
        chunks = []
        while True:
            nl = self._rx_buffer.find(b"\n")
            if nl == -1:
                break
            line = bytes(self._rx_buffer[:nl + 1])
            del self._rx_buffer[:nl + 1]
            chunks.append(self._decode_rx(line))
        # 若长时间收不到换行，避免缓冲无限增长。
        if len(self._rx_buffer) > 4096:
            chunks.append(self._decode_rx(bytes(self._rx_buffer)))
            self._rx_buffer.clear()
        if chunks:
            self.dataReceived.emit("".join(chunks))

    @staticmethod
    def _decode_rx(raw):
        """把下位机原始字节解码成可读文本。

        RF 固件调试串是 GBK 编码，且历史上被有损转换过，夹带大量
        U+FFFD(EF BF BD) 损坏字节（GBK 解出来就是 锟斤拷）。这里先把连续的
        损坏字节压成一个 '?'，再按 GBK 解码：能读的中文/ASCII 正常显示，
        读不出的用 '?' 占位，避免整屏乱码。$IMU 遥测行是纯 ASCII，原样通过。
        """
        marker = bytes([0xEF, 0xBF, 0xBD])
        out = bytearray()
        i = 0
        n = len(raw)
        prev_marker = False
        while i < n:
            if raw[i] == 0xEF and raw[i:i + 3] == marker:
                if not prev_marker:
                    out.append(0x3F)  # '?'
                    prev_marker = True
                i += 3
                continue
            out.append(raw[i])
            prev_marker = False
            i += 1
        return bytes(out).decode("gbk", errors="replace").replace("�", "?")

    def _on_error(self, error):
        if error == QtSerialPort.QSerialPort.SerialPortError.NoError:
            return
        message = self._serial.errorString()
        if message:
            self.errorOccurred.emit(message)
        if error == QtSerialPort.QSerialPort.SerialPortError.ResourceError:
            self._serial.close()
            self.statusChanged.emit("串口连接已中断：%s" % message, False)

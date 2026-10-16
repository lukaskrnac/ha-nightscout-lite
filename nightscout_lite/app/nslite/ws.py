"""A minimal WebSocket client (RFC 6455), enough for Home Assistant's websocket API.

Standard library only, like the rest of the add-on: text messages, fragmentation, ping/pong and
close. No TLS, because the add-on talks to the Supervisor's proxy over the internal network.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import urllib.parse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class WebSocketClosed(ConnectionError):
    """The server closed the connection or the stream ended."""


class WebSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buffer = b""

    @classmethod
    def connect(cls, url: str, timeout: float = 20.0, headers: dict[str, str] | None = None) -> "WebSocket":
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "http"):
            raise ValueError(f"unsupported websocket scheme in {url!r}")
        host = parts.hostname or "localhost"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        client = cls(sock)
        head = client._read_until(b"\r\n\r\n")
        status_line, *header_lines = head.decode("iso-8859-1").split("\r\n")
        if " 101 " not in f"{status_line} ":
            sock.close()
            raise ConnectionError(f"websocket upgrade refused: {status_line}")
        received = {}
        for line in header_lines:
            if ":" in line:
                name, value = line.split(":", 1)
                received[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if received.get("sec-websocket-accept") != expected:
            sock.close()
            raise ConnectionError("websocket upgrade answered with a wrong accept key")
        return client

    # ------------------------------------------------------------------ io

    def settimeout(self, seconds: float | None) -> None:
        self.sock.settimeout(seconds)

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("connection closed during handshake")
            self._buffer += chunk
            if len(self._buffer) > 65536:
                raise ConnectionError("handshake response too large")
        head, self._buffer = self._buffer.split(marker, 1)
        return head

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise WebSocketClosed("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def recv_text(self) -> str:
        """The next text message; answers pings on the way.

        Raises socket.timeout only while idle, between messages. A timeout partway through a
        message would leave the stream out of step, so that counts as a lost connection.
        """
        parts: list[bytes] = []
        size = 0
        while True:
            try:
                first, second = self._read_exact(2)
            except socket.timeout:
                if parts or self._buffer:
                    raise ConnectionError("websocket stalled mid-message") from None
                raise
            try:
                fin, opcode, payload = self._read_frame(first, second)
            except socket.timeout:
                raise ConnectionError("websocket stalled mid-message") from None
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                raise WebSocketClosed("server closed the websocket")
            size += len(payload)
            if size > MAX_MESSAGE_BYTES:
                raise ConnectionError("websocket message too large")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8")

    def _read_frame(self, first: int, second: int) -> tuple[bool, int, bytes]:
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > MAX_MESSAGE_BYTES:
            raise ConnectionError("websocket message too large")
        mask = self._read_exact(4) if second & 0x80 else None
        payload = self._read_exact(length)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

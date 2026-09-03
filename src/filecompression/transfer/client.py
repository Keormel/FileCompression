from __future__ import annotations

import asyncio

from .protocol import receive_frame, receive_message, send_frame, send_message


class TransferClient:
    def __init__(self, host: str, port: int, user: str) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        try:
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            await send_message(self.writer, {"type": "register", "user": self.user})
            response = await receive_message(self.reader)
            if response.get("type") != "registered":
                raise ConnectionError(response.get("message", "Registration failed"))
        except Exception:
            await self.close()
            raise

    async def send(self, recipient: str, container: bytes) -> None:
        self._connected()
        await send_message(self.writer, {"type": "send", "recipient": recipient})
        await send_frame(self.writer, container)
        response = await receive_message(self.reader)
        if response.get("type") != "sent":
            raise ConnectionError(response.get("message", "Transfer failed"))

    async def receive(self) -> bytes:
        self._connected()
        await send_message(self.writer, {"type": "receive"})
        response = await receive_message(self.reader)
        if response.get("type") != "file":
            raise FileNotFoundError(response.get("message", "Inbox is empty"))
        container = await receive_frame(self.reader)
        await send_message(self.writer, {"type": "received"})
        return container

    async def list_incoming(self) -> int:
        self._connected()
        await send_message(self.writer, {"type": "list"})
        response = await receive_message(self.reader)
        if response.get("type") != "incoming":
            raise ConnectionError(response.get("message", "Unable to list inbox"))
        return int(response["count"])

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
            self.reader = None

    def _connected(self) -> None:
        if self.reader is None or self.writer is None:
            raise ConnectionError("Client is not connected")

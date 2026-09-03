from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .protocol import receive_frame, receive_message, send_frame, send_message


@dataclass
class _Inbox:
    containers: list[bytes] = field(default_factory=list)


class TransferServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._users: dict[str, _Inbox] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        if self._server.sockets:
            self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        username: str | None = None
        try:
            registration = await receive_message(reader)
            if registration.get("type") != "register" or not self._valid_user(registration.get("user")):
                await send_message(writer, {"type": "error", "message": "Registration required"})
                return
            username = registration["user"]
            self._users.setdefault(username, _Inbox())
            await send_message(writer, {"type": "registered", "user": username})
            while True:
                request = await receive_message(reader)
                if request.get("type") == "send":
                    recipient = request.get("recipient")
                    if not self._valid_user(recipient) or recipient not in self._users:
                        await send_message(writer, {"type": "error", "message": "Recipient is not connected"})
                        continue
                    container = await receive_frame(reader)
                    self._users[recipient].containers.append(container)
                    await send_message(writer, {"type": "sent", "size": len(container)})
                elif request.get("type") == "list":
                    await send_message(writer, {"type": "incoming", "count": len(self._users[username].containers)})
                elif request.get("type") == "receive":
                    if not self._users[username].containers:
                        await send_message(writer, {"type": "error", "message": "Inbox is empty"})
                        continue
                    container = self._users[username].containers.pop(0)
                    await send_message(writer, {"type": "file", "size": len(container)})
                    await send_frame(writer, container)
                else:
                    await send_message(writer, {"type": "error", "message": "Unknown request"})
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _valid_user(value: object) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= 64 and value.isidentifier()

import pytest

from filecompression.container import pack, unpack
from filecompression.transfer import TransferClient, TransferServer


@pytest.mark.asyncio
async def test_two_user_transfer() -> None:
    server = TransferServer(port=0)
    await server.start()
    sender = TransferClient("127.0.0.1", server.port, "user_a")
    receiver = TransferClient("127.0.0.1", server.port, "user_b")
    await sender.connect()
    await receiver.connect()
    source = b"transfer test" * 100
    await sender.send("user_b", pack(source, "transfer.bin", "huffman"))
    received = unpack(await receiver.receive())
    assert received.decompress() == source
    await sender.close()
    await receiver.close()
    await server.stop()


@pytest.mark.asyncio
async def test_connection_refused_and_empty_inbox() -> None:
    client = TransferClient("127.0.0.1", 1, "user_a")
    with pytest.raises((ConnectionRefusedError, TimeoutError, OSError)):
        await client.connect()

    server = TransferServer(port=0)
    await server.start()
    client = TransferClient("127.0.0.1", server.port, "user_a")
    await client.connect()
    with pytest.raises(FileNotFoundError):
        await client.receive()
    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_invalid_recipient_does_not_desynchronise_connection() -> None:
    server = TransferServer(port=0)
    await server.start()
    sender = TransferClient("127.0.0.1", server.port, "user_a")
    await sender.connect()
    with pytest.raises(ConnectionError, match="Recipient is not connected"):
        await sender.send("user_b", b"invalid")
    with pytest.raises(FileNotFoundError):
        await sender.receive()
    await sender.close()
    await server.stop()


@pytest.mark.asyncio
async def test_server_rejects_invalid_container_and_duplicate_user() -> None:
    server = TransferServer(port=0)
    await server.start()
    sender = TransferClient("127.0.0.1", server.port, "user_a")
    receiver = TransferClient("127.0.0.1", server.port, "user_b")
    await sender.connect()
    await receiver.connect()
    with pytest.raises(ConnectionError, match="truncated"):
        await sender.send("user_b", b"not-an-fcmp")
    duplicate = TransferClient("127.0.0.1", server.port, "user_a")
    with pytest.raises(ConnectionError, match="already connected"):
        await duplicate.connect()
    await sender.close()
    await receiver.close()
    await duplicate.close()
    await server.stop()

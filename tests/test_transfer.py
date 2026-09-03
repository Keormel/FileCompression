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

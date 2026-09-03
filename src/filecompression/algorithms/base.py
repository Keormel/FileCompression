from abc import ABC, abstractmethod


class CompressionAlgorithm(ABC):
    """Binary codec contract used by the container and application layers."""

    name: str

    @abstractmethod
    def compress(self, data: bytes) -> tuple[bytes, bytes]:
        """Return compressed payload and decoder metadata."""

    @abstractmethod
    def decompress(self, payload: bytes, metadata: bytes) -> bytes:
        """Restore the original bytes from payload and metadata."""

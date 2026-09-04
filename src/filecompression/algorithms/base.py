from abc import ABC, abstractmethod
from collections.abc import Callable

MAX_DECOMPRESSED_SIZE = 256 * 1024 * 1024
ProgressCallback = Callable[[int, int], None]


class CompressionAlgorithm(ABC):
    """Binary codec contract used by the container and application layers."""

    name: str

    @abstractmethod
    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        """Return compressed payload and decoder metadata."""

    @abstractmethod
    def decompress(self, payload: bytes, metadata: bytes) -> bytes:
        """Restore the original bytes from payload and metadata."""

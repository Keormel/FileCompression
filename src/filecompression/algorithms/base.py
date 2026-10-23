from abc import ABC, abstractmethod
from collections.abc import Callable

# Upper bound for a single codec call. Large files are split into blocks by the
# container, so each block stays far below this limit.
MAX_DECOMPRESSED_SIZE = 256 * 1024 * 1024
ProgressCallback = Callable[[int, int], None]


class CompressionAlgorithm(ABC):
    """Binary codec contract used by the container and application layers."""

    name: str

    @abstractmethod
    def compress(self, data: bytes, progress: ProgressCallback | None = None) -> tuple[bytes, bytes]:
        """Return compressed payload and decoder metadata."""

    @abstractmethod
    def decompress(self, payload: bytes, metadata: bytes, limit: int = MAX_DECOMPRESSED_SIZE) -> bytes:
        """Restore the original bytes; refuse to produce more than ``limit`` bytes."""

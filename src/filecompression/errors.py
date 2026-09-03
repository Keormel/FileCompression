class CompressionError(Exception):
    """Base exception for expected compression failures."""


class CorruptDataError(CompressionError):
    """Raised when compressed data cannot be decoded safely."""


class ContainerError(CompressionError):
    """Raised when a container is invalid or unsupported."""

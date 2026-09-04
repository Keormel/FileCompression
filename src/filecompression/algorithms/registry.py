from .base import CompressionAlgorithm


def algorithms() -> dict[str, CompressionAlgorithm]:
    """Return the available codecs, imported lazily to avoid circular imports."""
    from .huffman import HuffmanAlgorithm
    from .lzw import LzwAlgorithm
    from .rle import RleAlgorithm
    from .stored import StoredAlgorithm

    return {
        "huffman": HuffmanAlgorithm(),
        "lzw": LzwAlgorithm(),
        "rle": RleAlgorithm(),
        "stored": StoredAlgorithm(),
    }


def get_algorithm(name: str) -> CompressionAlgorithm:
    try:
        return algorithms()[name.lower()]
    except KeyError as error:
        raise ValueError(f"Unknown compression algorithm: {name}") from error

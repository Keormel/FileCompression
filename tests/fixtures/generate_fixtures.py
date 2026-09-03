from __future__ import annotations

import base64
import json
from pathlib import Path


PNG_1X1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
JPEG_1X1 = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/AV//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AV//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/Aqf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IV//2gAMAwEAAgADAAAAEP/EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQMBAT8QH//EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQIBAT8QH//EABQQAQAAAAAAAAAAAAAAAAAAABD/2gAIAQEAAT8QH//Z")
PDF_1_PAGE = b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R >> endobj\n4 0 obj << /Length 0 >> stream\n\nendstream endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def generate(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "text_small.txt").write_text("Short compression fixture.\n" * 40, encoding="utf-8")
    (directory / "text_large.txt").write_text(("A" * 500 + "B" * 250 + "\n") * 400, encoding="utf-8")
    (directory / "data.json").write_text(json.dumps({"items": [{"id": i, "value": "sample"} for i in range(2000)]}), encoding="utf-8")
    (directory / "data.csv").write_text("id,value\n" + "\n".join(f"{i},sample" for i in range(2000)), encoding="utf-8")
    (directory / "binary.bin").write_bytes(bytes((i * 73 + 19) % 256 for i in range(100_000)))
    (directory / "image.png").write_bytes(PNG_1X1)
    (directory / "image.jpg").write_bytes(JPEG_1X1)
    (directory / "document.pdf").write_bytes(PDF_1_PAGE)


if __name__ == "__main__":
    generate(Path(__file__).parent)

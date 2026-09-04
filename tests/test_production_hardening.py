import pytest

from filecompression.container.format import validate_filename
from filecompression.errors import ContainerError


@pytest.mark.parametrize("filename", ["../secret.txt", "..\\secret.txt", "folder/file.txt", "bad\x00name.txt"])
def test_container_filename_rejects_path_and_control_characters(filename: str) -> None:
    with pytest.raises(ContainerError):
        validate_filename(filename)


def test_container_filename_accepts_normal_filename() -> None:
    assert validate_filename("report.json") == "report.json"

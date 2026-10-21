import hashlib
from pathlib import Path

import pytest

from sculptcut.fixtures import KINDS, generate_fixture
from sculptcut.media import probe


@pytest.mark.parametrize("kind", KINDS)
def test_fixture_metadata_and_determinism(tmp_path: Path, kind: str) -> None:
    first = generate_fixture(tmp_path / "первый файл.mp4", kind, 1)
    second = generate_fixture(tmp_path / "second.mp4", kind, 1)
    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    metadata = probe(first)
    assert metadata.duration_us is not None
    assert metadata.audio_streams == ([1] if kind == "audio" else [])
    if kind == "rotation":
        assert abs(metadata.rotation) == 90
    with pytest.raises(ValueError, match="exists"):
        generate_fixture(first, kind, 1)

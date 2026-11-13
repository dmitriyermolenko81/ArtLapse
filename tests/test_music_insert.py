"""Adding music preserves existing clips and fills only the free interval."""
from pathlib import Path

import pytest

from sculptcut.music_edit import MusicClip, insert_music_clip


def test_insert_fits_gap_and_preserves_following_clips():
    old = [MusicClip(path=Path('old.wav'), start_us=100, end_us=1100, at_us=3000)]
    new = MusicClip(path=Path('new.wav'), start_us=0, end_us=9000, at_us=1000)
    result = insert_music_clip(old, new)
    assert result == [new.model_copy(update={'end_us': 2000}), *old]
    assert old[0].at_us == 3000


@pytest.mark.parametrize('at', [3000, 3500, 3999])
def test_insert_on_occupied_music_is_rejected_without_changing_it(at):
    old = [MusicClip(path=Path('old.wav'), start_us=100, end_us=1100, at_us=3000)]
    before = list(old)
    with pytest.raises(ValueError, match='Выберите свободный промежуток'):
        insert_music_clip(old, MusicClip(path=Path('new.wav'), start_us=0,
                                       end_us=9000, at_us=at))
    assert old == before

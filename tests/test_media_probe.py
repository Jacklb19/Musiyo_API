import pytest

from scripts.media_probe import probe_audio


def test_mp3_probe_counts_actual_frames_after_metadata_and_rejects_truncation(tmp_path):
    frame = b'\xff\xfb\x90\xc0' + bytes(413)
    path = tmp_path / 'synthetic.mp3'
    path.write_bytes(b'ID3\x04\x00\x00\x00\x00\x00\x04' + b'test' + frame * 3)
    result = probe_audio(path, 'audio/mpeg')
    assert result['duration_seconds'] == pytest.approx(3456 / 44100)
    assert result['channels'] == 1 and result['sample_rate'] == 44100
    assert result['bitrates'] == [128000] and result['frames'] == 3
    for data in [frame[:-1], b'ID3\x04\x00\x00\x00\x00\x00\x7f', frame + b'invalid', bytes(10)]:
        path.write_bytes(data)
        with pytest.raises(ValueError):
            probe_audio(path, 'audio/mpeg')
    assert probe_audio(path, 'audio/ogg') is None

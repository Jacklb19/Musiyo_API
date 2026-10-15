"""Bounded structural probes for local audio; no decoding or speech recognition."""
import wave
from pathlib import Path


def probe_audio(path: Path, mime: str) -> dict | None:
    if mime == "audio/wav":
        with wave.open(str(path), "rb") as audio:
            return dict(duration_seconds=audio.getnframes() / audio.getframerate(),
                        sample_rate=audio.getframerate(), channels=audio.getnchannels(), method="wave_frame_count")
    if mime != "audio/mpeg":
        return None
    if path.stat().st_size > 3_000_000:
        raise ValueError("Audio exceeds the existing resource budget")
    data = path.read_bytes()
    position = 0
    if data[:3] == b"ID3":
        if len(data) < 10 or data[3] not in (2, 3, 4) or any(value & 128 for value in data[6:10]):
            raise ValueError("Invalid ID3 header")
        position = 10 + sum(value << (7 * (3 - index)) for index, value in enumerate(data[6:10]))
        if data[3] == 4 and data[5] & 16:
            position += 10
        if position > len(data):
            raise ValueError("Truncated ID3 tag")
    metadata_bytes = position
    end = len(data) - 128 if data[-128:-125] == b"TAG" else len(data)
    frames, seconds = 0, 0.0
    formats, bitrates = set(), set()
    while position < end:
        if position + 4 > end:
            raise ValueError("Truncated MPEG header")
        header = int.from_bytes(data[position:position + 4], "big")
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, rate_index = (header >> 12) & 15, (header >> 10) & 3
        if header >> 21 != 2047 or version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
            raise ValueError("Expected contiguous supported MPEG Layer III frames")
        sample_rate = (44100, 48000, 32000)[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
        rates = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3
                 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
        bitrate = rates[bitrate_index] * 1000
        size = (144 if version == 3 else 72) * bitrate // sample_rate + ((header >> 9) & 1)
        if position + size > end:
            raise ValueError("Truncated MPEG frame")
        formats.add((sample_rate, 1 if (header >> 6) & 3 == 3 else 2))
        bitrates.add(bitrate)
        frames += 1
        seconds += (1152 if version == 3 else 576) / sample_rate
        position += size
    if not frames or len(formats) != 1:
        raise ValueError("Missing audio frames or changing stream format")
    sample_rate, channels = next(iter(formats))
    return dict(duration_seconds=seconds, sample_rate=sample_rate, channels=channels,
                bitrates=sorted(bitrates), frames=frames, leading_metadata_bytes=metadata_bytes,
                method="mpeg_layer_iii_frame_scan", limits="Structural frame timing; decoding, transcript and pronunciation require independent review")

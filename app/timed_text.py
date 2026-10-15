"""The supported plain-text WebVTT subset for narration sentence cues."""
import re


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    if not blocks or blocks[0] != "WEBVTT":
        raise ValueError("Expected a plain WebVTT header")
    cues: list[tuple[float, float, str]] = []
    timestamp = r"(\d{2,}):([0-5]\d):([0-5]\d)\.(\d{3})"
    for block in blocks[1:]:
        lines = block.splitlines()
        if lines and "-->" not in lines[0]:
            lines = lines[1:]
        match = re.fullmatch(timestamp + " --> " + timestamp, lines[0]) if lines else None
        if match is None or len(lines) < 2:
            raise ValueError("Invalid WebVTT cue")
        values = list(map(int, match.groups()))
        start, end = [values[i] * 3600 + values[i + 1] * 60 + values[i + 2] + values[i + 3] / 1000 for i in (0, 4)]
        content = " ".join(lines[1:])
        if end <= start or cues and start < cues[-1][1] or "<" in content or "-->" in content:
            raise ValueError("Overlapping, empty or non-plain cue")
        cues.append((start, end, content))
    if not cues:
        raise ValueError("At least one measured cue is required")
    return cues

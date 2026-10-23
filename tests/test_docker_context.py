"""Every file the Dockerfile copies must survive .dockerignore, or the build fails on Render and the
previous version keeps running unnoticed (22/10/2026: deployment/survey was left out)."""
import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def kept(path: str, rules: list[str]) -> bool:
    keep = True
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.rstrip("/*") + "/*") or path == pattern.rstrip("/*"):
            keep = negate
    return keep


def test_every_copied_source_is_in_the_docker_context():
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    sources = []
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*COPY\s+(?!--from)(.+)", line)
        if match:
            sources += match.group(1).split()[:-1]
    assert sources
    for source in sources:
        files = [ROOT / source] if (ROOT / source).is_file() else [p for p in (ROOT / source).rglob("*") if p.is_file()]
        assert files, f"{source} does not exist"
        for file in files:
            relative = file.relative_to(ROOT).as_posix()
            assert kept(relative, rules), f"{relative} is copied by the Dockerfile but excluded by .dockerignore"

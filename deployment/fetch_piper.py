"""Image build step: fetch the Piper Linux runtime and the es_AR daniela voice, verified by SHA-256.

Used by the Dockerfile so the server can fall back to a local voice when ElevenLabs is unavailable. The
voice is trained on OpenSLR SLR61 (CC BY-SA 4.0); its attribution is shown on the privacy page and in the
museum credits. A changed upstream file stops the build instead of being installed.
"""
import hashlib
import sys
import tarfile
import urllib.request
from pathlib import Path

FILES = [
    ("https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz",
     "a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992", "piper_linux_x86_64.tar.gz"),
    ("https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_AR/daniela/high/es_AR-daniela-high.onnx",
     "7ceb1fc0dab349418c5b54a639ae9ee595212d7c9ea422220d8419163d5cc985", "voices/es_AR-daniela-high.onnx"),
    ("https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_AR/daniela/high/es_AR-daniela-high.onnx.json",
     "aedbf69647e1d754c62ecf8e0366ca5f16af3e768e3c6b5329af6eb6bde3852b", "voices/es_AR-daniela-high.onnx.json"),
]


def fetch(url: str, digest: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Musiyo-build/1.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit(f"Checksum mismatch for {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for url, digest, name in FILES:
        fetch(url, digest, root / name)
    archive = root / "piper_linux_x86_64.tar.gz"
    with tarfile.open(archive) as bundle:
        bundle.extractall(root, filter="data")  # creates root/piper/
    archive.unlink()
    (root / "piper" / "piper").chmod(0o755)


if __name__ == "__main__":
    main(Path(sys.argv[1]))

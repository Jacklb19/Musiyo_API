import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_destinations_are_required_and_all_are_validated_first(tmp_path):
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'package.json').write_text('{}')
    def run(*args):
        return subprocess.run([sys.executable, '-m', 'scripts.sync_contracts', *args], cwd=ROOT, capture_output=True)
    assert run('--target', 'web').returncode != 0
    assert run('--target', 'web', '--web', str(tmp_path / 'missing')).returncode != 0
    assert run('--target', 'all', '--web', str(web), '--unity', str(tmp_path / 'missing')).returncode != 0
    assert not (web / 'contracts').exists()
    assert run('--target', 'web', '--web', str(web)).returncode == 0
    assert (web / 'contracts/source.sha256').is_file()
    for source in (ROOT / 'contracts').rglob('*.json'):
        copy = (web / 'contracts' / source.relative_to(ROOT / 'contracts')).read_bytes()
        # Copies are LF even from a Windows checkout with CRLF, so their recorded hashes hold everywhere.
        assert copy == source.read_bytes().replace(b'\r\n', b'\n') and b'\r\n' not in copy
    for line in (web / 'contracts/source.sha256').read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256((web / 'contracts' / name).read_bytes()).hexdigest() == digest

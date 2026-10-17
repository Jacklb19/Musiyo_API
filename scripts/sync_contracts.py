"""Copy canonical API contracts to explicitly selected existing client checkouts."""
import argparse
import hashlib
import shutil
from pathlib import Path

from scripts.generate_unity_contracts import generate

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'contracts'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('web', 'unity', 'all'), required=True)
    parser.add_argument('--web', type=Path, help='Existing Web checkout root')
    parser.add_argument('--unity', type=Path, help='Existing Unity checkout root')
    args = parser.parse_args()
    selected = ('web', 'unity') if args.target == 'all' else (args.target,)
    targets = {}
    for name in selected:
        path = getattr(args, name)
        if path is None or not path.is_dir():
            parser.error(f'--{name} must select an existing checkout')
        path = path.resolve()
        marker = path / ('package.json' if name == 'web' else 'Assets')
        if not marker.exists() or path == ROOT or path.is_relative_to(SOURCE):
            parser.error(f'Invalid {name} checkout')
        if name == 'unity' and not (path / 'Assets/Scripts/Museo').is_dir():
            parser.error('Unity generated model directory must already exist')
        targets[name] = path / ('contracts' if name == 'web' else 'Assets/Contracts')
    files = sorted(path for path in SOURCE.rglob('*.json') if path.is_file())
    if not files:
        parser.error('No generated contracts found. Run scripts.build_contracts first.')
    manifest = ''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(SOURCE).as_posix()}\n' for path in files)
    # Validate every destination and generate models before writing any client.
    unity_code = generate() if 'unity' in targets else None
    for name, target in targets.items():
        for source in files:
            destination = target / source.relative_to(SOURCE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        (target / 'source.sha256').write_text(manifest, encoding='utf-8', newline='\n')
        if name == 'unity':
            (target.parent / 'Scripts/Museo/PublicContractModelsV1.cs').write_text(unity_code, encoding='utf-8', newline='\n')
        print(target)


if __name__ == '__main__':
    main()

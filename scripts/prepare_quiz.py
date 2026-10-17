"""Create private unreviewed quiz drafts from a read-only selected database."""
import argparse
from pathlib import Path

from app.db import make_session_factory
from app.quiz_service import generate_drafts, write_new_bundle

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--tour', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    database, output = args.database.resolve(strict=True), args.output.resolve()
    if output.is_relative_to(ROOT) or database.is_relative_to(ROOT):
        parser.error('Private database and quiz drafts must stay outside the repository')
    url = 'sqlite:///file:' + database.as_posix() + '?mode=ro&uri=true'
    factory = make_session_factory(url)
    try:
        bundle = generate_drafts(factory, args.tour)
        write_new_bundle(output, bundle)
        print(f'{len(bundle.questions)} draft questions; none reviewed or published')
    finally:
        factory.kw['bind'].dispose()


if __name__ == '__main__':
    main()

"""Read selected local session inputs without shell-embedded Python or mutations."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.guide_config import GuideSettings
from app.quiz_service import QuizBundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('guide', 'database', 'quiz'))
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    if args.kind == 'quiz':
        QuizBundle.model_validate_json(args.path.read_text(encoding='utf-8-sig'))
        return
    if args.kind == 'guide':
        GuideSettings.model_validate_json(args.path.read_text(encoding='utf-8-sig'))
        return
    with sqlite3.connect(args.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.create_function('musiyo_now', 0, lambda: datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f'))
        if db.execute('select version_num from alembic_version').fetchone() != ('0005',):
            raise ValueError('Selected database needs an explicit migration')
        print(json.dumps(sorted(row[0] for row in db.execute('select id from v_current_elements'))))


if __name__ == '__main__':
    main()

"""Export the anonymous closing-conversation answers for the thesis (read-only).

Writes two CSV files into a NEW folder outside the repositories:
  respuestas.csv  one row per stored conversation: day, platform, survey version and one column per question;
  totales.csv     per question and option, how many answered it, overall and per platform, in the order
                  the options are offered (so ordered scales stay ordered).
The target is the local SQLite museum (--database) or the online database (--database-url, without a value
it reads MUSIYO_DATABASE_URL). Nothing is written to the database.
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import sqlalchemy as sa

from app.survey_contracts import GuideSurvey
from app.survey_service import DEFAULT_BUNDLE, SURVEY_RESPONSES
from scripts.correction_target import ENVIRONMENT, label, selected
from scripts.local_backup import new_external_folder


def export(url: str, survey: GuideSurvey, folder: Path) -> dict:
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as db:
            rows = db.execute(sa.select(SURVEY_RESPONSES).where(SURVEY_RESPONSES.c.survey_key == survey.survey_key)
                              .order_by(SURVEY_RESPONSES.c.answered_on, SURVEY_RESPONSES.c.id)).mappings().all()
    finally:
        engine.dispose()
    folder.mkdir()
    questions = [question.id for question in survey.questions]
    with (folder / "respuestas.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["dia", "plataforma", "version", *questions])
        for row in rows:
            answers = row["answers"] if isinstance(row["answers"], dict) else json.loads(row["answers"])
            writer.writerow([row["answered_on"], row["platform"] or "", row["version"], *(answers.get(q, "") for q in questions)])
    platforms = sorted({row["platform"] or "sin dato" for row in rows})
    counts: dict[tuple[str, str, str], int] = Counter()
    for row in rows:
        answers = row["answers"] if isinstance(row["answers"], dict) else json.loads(row["answers"])
        for question, option in answers.items():
            counts[(question, option, "total")] += 1
            counts[(question, option, row["platform"] or "sin dato")] += 1
    with (folder / "totales.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["pregunta", "texto", "orden", "opcion", "texto_opcion", "total", *platforms])
        for question in survey.questions:
            for order, option in enumerate(question.options, start=1):
                writer.writerow([question.id, question.text, order, option.id, option.text,
                                 counts[(question.id, option.id, "total")],
                                 *(counts[(question.id, option.id, platform)] for platform in platforms)])
    return dict(conversations=len(rows), platforms=platforms, folder=str(folder))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--database")
    where.add_argument("--database-url", nargs="?", const=ENVIRONMENT, metavar="URL")
    parser.add_argument("--survey", default=str(DEFAULT_BUNDLE), help="Survey bundle the answers belong to")
    parser.add_argument("--output-folder", required=True, help="A NEW folder outside the repositories")
    arguments = parser.parse_args()
    target = selected(arguments)
    survey = GuideSurvey.model_validate(json.loads(Path(arguments.survey).read_text(encoding="utf-8-sig")))
    result = export(target, survey, new_external_folder(arguments.output_folder))
    print(json.dumps(dict(result, source=label(target)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

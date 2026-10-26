"""The guide's manners: greetings, thanks and questions the fichas cannot answer, without AI.

A visitor talks to the guide as to a person («Hola, ¿qué tal? ¿Me cuentas del museo?»). Courtesy is
split off before retrieval so it neither dilutes the search nor reaches Groq, and the guide answers
it briefly itself. When no published ficha supports an answer, the guide says so plainly and offers
three current records instead of a technical failure, so it never talks about anything else.
Approved by the owner on 26/10/2026: «que responda preguntas referentes al museo, de manera natural,
concisa… como una persona. Si la persona le pregunta de otra cosa, que no responda de eso».
"""
import hashlib
import re

# Leading courtesy, accents optional; repeated so «Hola, buenas, ¿qué tal?» is removed entirely.
GREETING = (r"hola+|holi|buen[oa]s(?: d[ií]as| tardes| noches)?|hey|oye|saludos|qu[eé] tal|qu[eé] m[aá]s"
            r"|c[oó]mo (?:est[aá]s?|est[aá]n|te va|vas|va todo)|mucho gusto|encantad[oa]")
THANKS = r"(?:muchas )?gracias|por favor|disculpa|perd[oó]n"
COURTESY = re.compile(rf"^\s*(?:(?:{GREETING}|{THANKS})(?:\s+(?:guía|amigo|amiga|colibr[ií]))?[\s,;:.!¡?¿]*)+",
                      re.IGNORECASE)
GREETED = re.compile(rf"^\s*(?:{GREETING})\b", re.IGNORECASE)
SEGMENT = re.compile(r"[^.?!]+[.?!]*")

WELCOME = "¡Hola! Soy tu guía en el museo. Pregúntame lo que quieras sobre el Bëtsknaté; por ejemplo, sobre {topics}."
REDIRECT = "Eso no lo encuentro en las fichas del museo, y prefiero no inventarlo. Puedo contarte sobre {topics}."
GREETING_PREFIX = "¡Hola! "


def split_courtesy(question: str) -> tuple[bool, str]:
    """Whether the visitor greeted, and the question without its courtesy («¿qué tal?», «gracias»…)."""
    greeted = False
    kept = []
    for segment in SEGMENT.findall(question.replace("¿", " ").replace("¡", " ")):
        greeted = greeted or bool(GREETED.match(segment))
        rest = COURTESY.sub("", segment).strip()
        if len(re.sub(r"[^\w]", "", rest)) >= 3:
            kept.append(rest)
    core = " ".join(kept).strip()
    if core and not core.endswith(("?", ".", "!")):
        core += "?"
    return greeted, core


def suggestions(titles: dict[str, str], seed: str, count: int = 3) -> list[str]:
    """Three current records to offer, varied by question; pieces and the credits record are left out."""
    pool = sorted(slug for slug, title in titles.items()
                  if not title.lower().startswith("máscara") and "crédito" not in title.lower())
    if not pool:
        return []
    start = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(pool)
    step = max(1, len(pool) // count)
    chosen: list[str] = []
    for offset in range(len(pool)):
        slug = pool[(start + offset * step) % len(pool)]
        if slug not in chosen:
            chosen.append(slug)
        if len(chosen) == count:
            break
    return chosen


def topics_text(slugs: list[str], titles: dict[str, str]) -> str:
    def spoken(title: str) -> str:
        # «El Matachín» reads «el Matachín» inside a sentence.
        first, _, rest = title.partition(" ")
        return (first.lower() + " " + rest) if rest and first in ("El", "La", "Los", "Las") else title
    names = [spoken(titles[slug]) for slug in slugs]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " o " + names[-1]

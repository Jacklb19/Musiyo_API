"""Portable local search; production uses PostgreSQL's Spanish text configuration."""
import re
import unicodedata


def fold(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char))


def trigrams(value: str) -> set[str]:
    result: set[str] = set()
    for word in re.findall(r"\w+", fold(value)):
        padded = "  " + word + " "
        result.update(padded[index:index + 3] for index in range(len(padded) - 2))
    return result


def similarity(left: str, right: str) -> float:
    a, b = trigrams(left), trigrams(right)
    return len(a & b) / len(a | b) if a or b else 0.0


def local_rank(title: str, terms: str, body: str, query: str) -> float:
    words = re.findall(r"\w+", fold(query))
    fields = [set(re.findall(r"\w+", fold(value))) for value in (title, terms, body)]
    # SQLite is an explicit development fallback, without Spanish stemming or web-search operators.
    if words and all(any(word in field for field in fields) for word in words):
        return sum(weight * sum(word in field for word in words)
            for field, weight in zip(fields, (4.0, 2.0, 1.0)))
    value = similarity(title, query)
    return value if value >= 0.3 else 0.0

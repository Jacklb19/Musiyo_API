"""Authorized, current evidence from the existing PostgreSQL/pgvector index."""
import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any, Literal

import sqlalchemy as sa

from .content_tables import CONTENT
from .db import utc_now
from .guide_adapters import Embeddings, LocalHashEmbeddings
from .guide_relevance import (
    coverage,
    is_identity_question,
    matching_terms,
    question_terms,
)
from .public_repository import ASSISTANT_ELEMENTS, PublicRepository


@dataclass(frozen=True)
class Evidence:
    fragment_id: str
    element_slug: str
    title: str
    community: str | None
    block_id: str | None
    kind: Literal['summary', 'documented_fact', 'testimony', 'interpretation']
    text: str
    source_id: str | None
    source_reference: str | None
    source_url: str | None
    attribution: str | None
    context: str | None
    credits: tuple[tuple[str | None, str], ...]

    @property
    def indexed_text(self) -> str:
        # Metadata is part of the fingerprint, so a correction invalidates stale evidence.
        return json.dumps({key: value for key, value in self.__dict__.items() if key != 'fragment_id'},
                          ensure_ascii=False, sort_keys=True)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.indexed_text.encode()).hexdigest()


def chunks(text: str):
    start = 0
    while start < len(text):
        end = min(start + 800, len(text))
        if end < len(text):
            boundary = text.rfind(' ', start + 650, end)
            if boundary > start:
                end = boundary
        yield text[start:end].strip()
        if end == len(text):
            break
        start = end - 100


def record_evidence(record):
    sources = {source.id: source for source in record.sources}
    community = record.community.name if record.community else None
    credits = tuple((credit.role, credit.name) for credit in record.credits)
    blocks = [(block.id, block.kind, block.text, block.source_id, block.attribution, block.context)
              for block in record.blocks if block.id != 'legacy-interpretation']
    if record.sources and record.description.strip():
        # A summary cites the record, without inventing a source-to-description correspondence.
        blocks.insert(0, (None, 'summary', record.description, None, None, None))
    for block_id, kind, text, source_id, attribution, context in blocks:
        source = sources.get(source_id)
        for chunk in chunks(text):
            yield Evidence('', record.slug, record.title, community, block_id, kind, chunk, source_id,
                           source.reference if source else None, source.url if source else None,
                           attribution, context, credits)


def index_evidence(db, element_id=None, now=utc_now, embeddings: Embeddings | None = None):
    public = PublicRepository(db, now)
    adapter = embeddings or LocalHashEmbeddings()
    if adapter.dimension != 1024:
        raise ValueError("The current pgvector schema requires 1024 dimensions")
    table = CONTENT['index_fragments']
    allowed = set(db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id)))
    if element_id is not None:
        allowed &= {element_id}
    db.execute(table.delete().where(table.c.element_id == element_id if element_id else sa.true()))
    count = 0
    for slug in sorted(allowed):
        record = public.element(slug)
        if record is None:
            continue
        evidence = list(record_evidence(record))
        vectors = adapter.embed([item.indexed_text if isinstance(adapter, LocalHashEmbeddings) else
                                item.title + "\n" + item.text for item in evidence])
        if len(vectors) != len(evidence):
            raise ValueError("Embedding count differs from evidence")
        for item, vector in zip(evidence, vectors, strict=True):
            if len(vector) != 1024:
                raise ValueError("Embedding dimension differs from the schema")
            if any(not math.isfinite(number) for number in vector):
                raise ValueError("Embedding must contain finite values")
            values: dict[str, Any] = dict(element_id=slug, block_id=item.block_id, text=item.indexed_text,
                                        text_hash=item.text_hash, embedding=vector, embedding_model=adapter.model)
            values['search_vector'] = sa.func.to_tsvector(sa.literal_column("'public.spanish_unaccent'"),
                                                         item.text + ' ' + item.title) if db.bind.dialect.name == 'postgresql' else None
            db.execute(table.insert().values(**values))
            count += 1
    return count


def index_status(db, now=utc_now, embeddings: Embeddings | None = None) -> list[dict]:
    """Records whose guide fragments differ from what they currently publish, read-only.

    A record edited after indexing (its text, sources or credits are part of each fingerprint)
    keeps stale fragments that retrieval silently drops, so the guide abstains about it.
    """
    public = PublicRepository(db, now)
    model = (embeddings or LocalHashEmbeddings()).model
    table = CONTENT['index_fragments']
    indexed: dict[str, set[str]] = {}
    for row in db.execute(sa.select(table.c.element_id, table.c.text_hash).where(table.c.embedding_model == model)):
        indexed.setdefault(row.element_id, set()).add(row.text_hash)
    stale = []
    for slug in sorted(set(db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id))) | indexed.keys()):
        record = public.element(slug)
        expected = {item.text_hash for item in record_evidence(record)} if record else set()
        if expected != indexed.get(slug, set()):
            stale.append(dict(element=slug, indexed=len(indexed.get(slug, ())), current=len(expected & indexed.get(slug, set())),
                              expected=len(expected)))
    return stale


class GuideRepository:
    def __init__(self, db, now=utc_now):
        self.db, self.now = db, now
        self.public = PublicRepository(db, now)

    def current(self, rows) -> list[Evidence]:
        records = {}
        result = []
        allowed = set(self.db.scalars(sa.select(ASSISTANT_ELEMENTS.c.id)))
        for row in rows:
            slug = row['element_id']
            if slug not in allowed:
                continue
            if slug not in records:
                record = self.public.element(slug)
                records[slug] = {item.text_hash: item for item in record_evidence(record)} if record else {}
            item = records[slug].get(row['text_hash'])
            if item is not None and item.indexed_text == row['text']:
                result.append(replace(item, fragment_id=row['id']))
        return result

    def retrieve(self, question: str, element_slug: str | None, embeddings: Embeddings, settings) -> list[Evidence]:
        query_terms = question_terms(question)
        if not query_terms:
            return []
        identity = is_identity_question(question)
        table = CONTENT['index_fragments']
        base = sa.select(table).join(ASSISTANT_ELEMENTS, ASSISTANT_ELEMENTS.c.id == table.c.element_id)
        base = base.where(table.c.embedding_model == embeddings.model)
        if element_slug:
            base = base.where(table.c.element_id == element_slug)
        if self.db.execute(base.limit(1)).first() is None:
            return []
        query_embedding = getattr(embeddings, 'embed_query', None)
        vector = query_embedding(question) if query_embedding else embeddings.embed([question])[0]
        if self.db.bind.dialect.name == 'postgresql':
            distance = table.c.embedding.op('<=>')(sa.cast(sa.literal(json.dumps(vector)), table.c.embedding.type))
            semantic = self.db.execute(base.where(table.c.embedding.is_not(None))
                                       .order_by(distance, table.c.id).limit(settings.candidate_limit)).mappings().all()
            # Any term may match; ts_rank_cd ranks fragments covering more terms first. Requiring every term
            # (the websearch default) dropped the only fragment naming a rare word such as «Alguacero».
            query = sa.func.websearch_to_tsquery(sa.literal_column("'public.spanish_unaccent'"), ' or '.join(sorted(query_terms)))
            lexical = self.db.execute(base.where(table.c.search_vector.bool_op('@@')(query))
                                      .order_by(sa.func.ts_rank_cd(table.c.search_vector, query).desc(), table.c.id)
                                      .limit(settings.candidate_limit)).mappings().all()
        else:
            rows = self.db.execute(base.order_by(table.c.id)).mappings().all()
            semantic = sorted(rows, key=lambda row: -sum(a * b for a, b in zip(vector, row['embedding'] or [], strict=False)))[:settings.candidate_limit]
            # Rank actual authorized evidence, not serialized credits/source metadata.
            current = {item.fragment_id: item for item in self.current(rows)}
            lexical = sorted((row for row in rows if row['id'] in current),
                             key=lambda row: (-coverage(query_terms, current[row['id']].text + ' ' + current[row['id']].title),
                                              -int(identity and current[row['id']].kind == 'summary'),
                                              -coverage(query_terms, current[row['id']].title) if identity else 0,
                                              len(question_terms(current[row['id']].title)) if identity else 0,
                                              -coverage(query_terms, current[row['id']].text), row['id']))[:settings.candidate_limit]
        scores: dict[str, float] = {}
        candidates = {}
        for ranking in (lexical, semantic):
            for rank, row in enumerate(ranking):
                candidates[row['id']] = row
                scores[row['id']] = scores.get(row['id'], 0.0) + 1 / (60 + rank + 1)
        ordered = sorted(candidates, key=lambda key: (-scores[key], key))
        evidence = self.current([candidates[key] for key in ordered])
        # Hash collisions cannot establish sufficiency. Real model similarity is a
        # candidate gate, while generation must still abstain without direct support.
        similarities = {row['id']: sum(a * b for a, b in zip(vector, row['embedding'] or [], strict=False))
                        for row in candidates.values()}
        # Extractive answers cannot infer support for unmatched question qualifiers.
        # Preserve the semantic adapter's separate similarity gate and settings.
        minimum_coverage = (1.0 if settings.generation == 'local' and isinstance(embeddings, LocalHashEmbeddings)
                            else settings.minimum_coverage)
        evidence = [item for item in evidence
                    if coverage(query_terms, item.text + ' ' + item.title) >= minimum_coverage
                    or not isinstance(embeddings, LocalHashEmbeddings)
                    and similarities[item.fragment_id] >= settings.minimum_similarity]
        # Among candidates covering the same share of the question, a rarer term (a name such as «Alguacero»)
        # identifies the topic better than a common one («cabildo»).
        found = {item.fragment_id: matching_terms(query_terms, item.text + ' ' + item.title) for item in evidence}
        frequency = {term: sum(term in terms for terms in found.values()) for term in query_terms}
        rarity = {key: sum(1 / frequency[term] for term in terms) for key, terms in found.items()}
        if isinstance(embeddings, LocalHashEmbeddings):
            evidence.sort(key=lambda item: (-coverage(query_terms, item.text + ' ' + item.title), -rarity[item.fragment_id],
                                            -int(identity and item.kind == 'summary'),
                                            -coverage(query_terms, item.title) if identity else 0,
                                            len(question_terms(item.title)) if identity else 0,
                                            -coverage(query_terms, item.text),
                                            -scores[item.fragment_id], item.fragment_id))
        return evidence[:settings.evidence_limit]

    def revalidate(self, evidence: list[Evidence]) -> bool:
        # End the retrieval snapshot; PostgreSQL CURRENT_TIMESTAMP must advance too.
        self.db.rollback()
        self.public = PublicRepository(self.db, self.now)
        table = CONTENT['index_fragments']
        rows = self.db.execute(sa.select(table).where(table.c.id.in_([item.fragment_id for item in evidence]))).mappings().all()
        current = {item.fragment_id: item.text_hash for item in self.current(rows)}
        return all(current.get(item.fragment_id) == item.text_hash for item in evidence)

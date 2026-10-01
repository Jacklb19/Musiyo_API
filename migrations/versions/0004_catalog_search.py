"""Maintain catalog search documents independently of publication eligibility."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def document_select(dialect: str) -> str:
    aggregate = "string_agg" if dialect == "postgresql" else "group_concat"
    return f"""SELECT e.id AS element_id, coalesce(d.title,e.title) AS title,
        coalesce(m.technique,'') || ' ' || coalesce(CAST(m.materials AS TEXT),'') || ' ' ||
        coalesce(c.name,'') || ' ' || coalesce((SELECT {aggregate}(col.name,' ')
            FROM collections col JOIN collection_elements ce ON ce.collection_id=col.id
            WHERE ce.element_id=e.id),'') AS terms,
        coalesce(d.description,e.description) || ' ' ||
        CASE WHEN d.element_id IS NULL THEN coalesce(e.interpretation,'') ELSE '' END || ' ' ||
        coalesce((SELECT {aggregate}(b.text,' ') FROM detail_blocks b WHERE b.element_id=e.id),'') AS body
        FROM elements e LEFT JOIN element_details d ON d.element_id=e.id
        LEFT JOIN element_metadata m ON m.element_id=e.id LEFT JOIN categories c ON c.id=m.category_id"""


def affected(table: str, row: str) -> str:
    if table == "categories":
        return f"SELECT element_id FROM element_metadata WHERE category_id={row}.id"
    if table == "collections":
        return f"SELECT element_id FROM collection_elements WHERE collection_id={row}.id"
    return f"SELECT {row}.{'id' if table == 'elements' else 'element_id'}"


TABLES = ("elements", "element_details", "element_metadata", "detail_blocks",
    "collection_elements", "categories", "collections")


def upgrade():
    dialect = op.get_bind().dialect.name
    op.create_table("element_search", sa.Column("element_id", sa.String(100), sa.ForeignKey("elements.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False), sa.Column("terms", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("normalized_title", sa.Text()),
        sa.Column("search_vector", TSVECTOR() if dialect == "postgresql" else sa.Text()))
    document = document_select(dialect)
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("CREATE TEXT SEARCH CONFIGURATION public.spanish_unaccent (COPY = pg_catalog.spanish)")
        op.execute("ALTER TEXT SEARCH CONFIGURATION public.spanish_unaccent ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.spanish_stem")
        op.execute(f"""CREATE FUNCTION refresh_catalog_search(target text) RETURNS void LANGUAGE sql AS $$
            INSERT INTO element_search (element_id,title,terms,body,normalized_title,search_vector)
            SELECT element_id,title,terms,body,public.unaccent(lower(title)),
                setweight(to_tsvector('public.spanish_unaccent',title),'A') ||
                setweight(to_tsvector('public.spanish_unaccent',terms),'B') ||
                setweight(to_tsvector('public.spanish_unaccent',body),'C')
            FROM ({document}) doc WHERE element_id=target
            ON CONFLICT (element_id) DO UPDATE SET title=excluded.title,terms=excluded.terms,
                body=excluded.body,normalized_title=excluded.normalized_title,search_vector=excluded.search_vector;
            $$""")
        op.execute("SELECT refresh_catalog_search(id) FROM elements")
        op.execute("CREATE INDEX ix_catalog_search_vector ON element_search USING gin(search_vector)")
        op.execute("CREATE INDEX ix_catalog_search_title ON element_search USING gin(normalized_title gin_trgm_ops)")
        for table in TABLES:
            op.execute(f"""CREATE FUNCTION catalog_change_{table}() RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE target text;
                BEGIN
                    IF TG_OP <> 'INSERT' THEN
                        FOR target IN {affected(table, 'OLD')} LOOP PERFORM refresh_catalog_search(target); END LOOP;
                    END IF;
                    IF TG_OP <> 'DELETE' THEN
                        FOR target IN {affected(table, 'NEW')} LOOP PERFORM refresh_catalog_search(target); END LOOP;
                    END IF;
                    RETURN NULL;
                END; $$""")
            op.execute(f"CREATE TRIGGER catalog_change_{table} AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION catalog_change_{table}()")
    else:
        op.execute(f"INSERT INTO element_search (element_id,title,terms,body) {document}")
        for table in TABLES:
            for operation, rows in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
                statements = "".join(f"""INSERT INTO element_search (element_id,title,terms,body)
                    SELECT * FROM ({document}) doc WHERE element_id IN ({affected(table, row)})
                    ON CONFLICT(element_id) DO UPDATE SET title=excluded.title,terms=excluded.terms,body=excluded.body;"""
                    for row in rows)
                op.execute(f"CREATE TRIGGER catalog_change_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN {statements} END")


def downgrade():
    raise RuntimeError("Catalog search migration is forward-only")

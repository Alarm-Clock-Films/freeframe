"""Columns that request paths filter on have an index, in the database and on the model.

`asset_shares.asset_id` is why both are checked: the model declared its index
from the start and no migration ever created it, so every access check for a
non-member read the whole table. CI migrates the test database before the
suite runs, so the first test is a check of the migrations themselves.
"""
import pytest
from sqlalchemy import inspect, text

# (table, what the index is on)
EXPECTED = [
    ("annotations", "comment_id"),
    ("comment_attachments", "comment_id"),
    ("asset_shares", "asset_id"),
    ("asset_shares", "shared_with_user_id"),
    ("share_link_items", "asset_id"),
    ("share_link_items", "folder_id"),
    ("share_links", "asset_id"),
    ("media_files", "s3_key_raw"),
    ("users", "lower(email)"),
]


def _leading_columns(db, table):
    """What each index on `table` starts with, as Postgres writes it."""
    rows = db.execute(text(
        "SELECT pg_get_indexdef(i.indexrelid, 1, true) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "WHERE c.relname = :table AND c.relnamespace = 'public'::regnamespace"
    ), {"table": table}).all()
    return {row[0].replace('"', "").replace("::text", "") for row in rows}


@pytest.mark.parametrize("table,column", EXPECTED)
def test_the_migrated_database_has_the_index(real_db, table, column):
    assert column in _leading_columns(real_db, table)


@pytest.mark.parametrize("table,column", EXPECTED)
def test_the_model_declares_the_index(table, column):
    import apps.api.models  # noqa: F401 - registers every table
    from apps.api.database import Base

    declared = set()
    for index in Base.metadata.tables[table].indexes:
        first = list(index.expressions)[0]
        declared.add(getattr(first, "name", None) or str(first))

    assert column in declared


def test_every_single_column_index_a_model_declares_exists_in_the_database(real_db):
    """The drift that hid the asset_shares.asset_id gap: declared, never migrated."""
    import apps.api.models  # noqa: F401
    from apps.api.database import Base

    existing_tables = set(inspect(real_db.connection()).get_table_names())
    missing = []
    for table in Base.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        in_db = _leading_columns(real_db, table.name)
        for index in table.indexes:
            first = list(index.expressions)[0]
            name = getattr(first, "name", None) or str(first)
            if name not in in_db:
                missing.append(f"{table.name}.{name} ({index.name})")

    assert missing == []

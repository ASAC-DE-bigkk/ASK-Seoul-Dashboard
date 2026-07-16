from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.auth.models import Base


def test_auth_schema_compiles_for_primary_rdb_dialects():
    dialects = (sqlite.dialect(), postgresql.dialect(), mysql.dialect())
    for dialect in dialects:
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            assert "CREATE TABLE" in ddl
            for index in table.indexes:
                index_ddl = str(CreateIndex(index).compile(dialect=dialect))
                assert "CREATE" in index_ddl and "INDEX" in index_ddl

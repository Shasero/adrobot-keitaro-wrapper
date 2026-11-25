from alembic import command
from sqlalchemy import inspect

from app.db.session import create_db_engine


def test_migration_round_trip_and_schema(alembic_config, database_url):
    command.upgrade(alembic_config, "head")
    engine = create_db_engine(database_url)
    table = "managed_campaigns"

    try:
        schema = inspect(engine)
        columns = {column["name"]: column for column in schema.get_columns(table)}
        assert set(columns) == {
            "id", "remote_campaign_id", "name", "status", "created_at", "updated_at"
        }
        assert schema.get_pk_constraint(table)["constrained_columns"] == ["id"]
        assert {name for name, column in columns.items() if column["nullable"]} == {
            "remote_campaign_id"
        }
        assert [constraint["column_names"] for constraint in schema.get_unique_constraints(table)] == [
            ["remote_campaign_id"]
        ]
        assert columns["status"]["default"].strip("'") == "pending"
        for name in ("created_at", "updated_at"):
            assert columns[name]["default"].strip("()").upper() == "CURRENT_TIMESTAMP"
        command.check(alembic_config)

        command.downgrade(alembic_config, "base")
        assert not inspect(engine).has_table(table)
        command.upgrade(alembic_config, "head")
        assert inspect(engine).has_table(table)
        command.check(alembic_config)
    finally:
        engine.dispose()

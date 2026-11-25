from alembic import command
from sqlalchemy import inspect, text

from app.db.session import create_db_engine


def test_operation_migration_preserves_old_campaign(alembic_config, database_url):
    command.upgrade(alembic_config, "0001")
    engine = create_db_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO managed_campaigns(name) VALUES ('Legacy')"))
        for revision in ("head", "0001", "head"):
            if revision == "head":
                command.upgrade(alembic_config, revision)
                command.check(alembic_config)
            else:
                command.downgrade(alembic_config, revision)
            assert inspect(engine).has_table("campaign_operations") == (revision == "head")
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT name FROM managed_campaigns")) == "Legacy"
    finally:
        engine.dispose()

from contextlib import closing
from datetime import datetime

import pytest
from alembic import command
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import create_db_engine, get_db
from app.models import ManagedCampaign


@pytest.fixture
def db_request(alembic_config, client):
    command.upgrade(alembic_config, "head")
    return Request({"type": "http", "app": client.app})


@pytest.mark.parametrize("database_url", [
    "postgresql://localhost/adrobot",
    "mysql://localhost/adrobot",
])
def test_create_db_engine_rejects_non_sqlite(database_url):
    with pytest.raises(ValueError, match="SQLite"):
        create_db_engine(database_url)


def test_create_read_and_release_session(db_request):
    with closing(get_db(db_request)) as sessions:
        session = next(sessions)
        campaign = ManagedCampaign(name="Test Campaign")
        session.add(campaign)
        session.flush()
        campaign_id = campaign.id
        session.commit()

    with closing(get_db(db_request)) as sessions:
        session = next(sessions)
        campaign = session.get(ManagedCampaign, campaign_id)
        assert campaign.name == "Test Campaign"
        assert campaign.status == "pending"
        assert campaign.remote_campaign_id is None
        assert campaign.created_at == campaign.updated_at
        assert campaign.created_at.tzinfo is None
        pool = session.get_bind().pool
        assert pool.checkedout() == 1

    assert pool.checkedout() == 0
    assert not session.in_transaction()


def test_remote_id_is_unique_and_multiple_nulls_are_allowed(db_request):
    with closing(get_db(db_request)) as sessions:
        session = next(sessions)
        session.add_all(
            [
                ManagedCampaign(name="Same name"),
                ManagedCampaign(name="Same name"),
                ManagedCampaign(name="Published", remote_campaign_id=101),
            ]
        )
        session.commit()

        session.add(ManagedCampaign(name="Duplicate", remote_campaign_id=101))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        campaigns = session.scalars(select(ManagedCampaign).order_by(ManagedCampaign.id))
        assert [campaign.remote_campaign_id for campaign in campaigns] == [None, None, 101]


def test_name_cannot_be_blank(db_request):
    with closing(get_db(db_request)) as sessions:
        session = next(sessions)
        session.add(ManagedCampaign(name=" "))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_update_refreshes_timestamp(db_request):
    old_timestamp = datetime(2020, 1, 1)
    with closing(get_db(db_request)) as sessions:
        session = next(sessions)
        campaign = ManagedCampaign(name="Before", updated_at=old_timestamp)
        session.add(campaign)
        session.commit()
        created_at = campaign.created_at
        assert campaign.updated_at == old_timestamp

        campaign.name = "After"
        session.commit()

        assert campaign.created_at == created_at
        assert campaign.updated_at >= created_at

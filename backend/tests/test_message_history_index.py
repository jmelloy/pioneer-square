"""Exercise the real migration and latest-100 conversation-history query plan."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from helpers import _sync_session, insert_conversation, insert_guild
from models import Guild, Message
from sqlalchemy import inspect, text
from sqlmodel import col, select

_INDEX = "ix_messages_conversation_id_created_at_id"


def test_message_history_index_migration_round_trip(client):
    _, db_url = client
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    def indexes():
        with _sync_session(db_url) as session:
            return {
                index["name"]: index["column_names"]
                for index in inspect(session.connection()).get_indexes("messages")
            }

    assert indexes()[_INDEX] == ["conversation_id", "created_at", "id"]
    try:
        command.downgrade(cfg, "20261026_000000")
        assert _INDEX not in indexes()
        assert indexes()["ix_messages_conversation_id"] == ["conversation_id"]
    finally:
        command.upgrade(cfg, "head")
    assert indexes()[_INDEX] == ["conversation_id", "created_at", "id"]
    assert "ix_messages_conversation_id" not in indexes()
    model_index = next(index for index in Message.__table__.indexes if index.name == _INDEX)
    assert [column.name for column in model_index.columns] == indexes()[_INDEX]


def test_latest_conversation_messages_do_not_sort_full_history(client):
    _, db_url = client
    insert_guild(db_url, "g-message-index")
    conversation_id = insert_conversation(db_url, "g-message-index")
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == "g-message-index"))
        session.execute(
            text(
                "INSERT INTO messages (guild_id, conversation_id, content, message_type, created_at) "
                "SELECT :guild, :conversation, repeat('x', 100), 'chat', "
                "now() - (n / 10) * interval '1 second' FROM generate_series(1, 16000) n"
            ),
            {"guild": guild_pk, "conversation": conversation_id},
        )
        session.execute(text("ANALYZE messages"))
        stmt = (
            select(Message)
            .where(col(Message.conversation_id) == conversation_id)
            .order_by(col(Message.created_at).desc(), col(Message.id).desc())
            .limit(100)
        )
        sql = str(stmt.compile(session.get_bind(), compile_kwargs={"literal_binds": True}))
        plan = session.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql)).scalar_one()[0]

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        query_nodes = list(nodes(plan["Plan"]))
        assert any(node.get("Index Name") == _INDEX for node in query_nodes)
        assert not any("Sort" in node["Node Type"] for node in query_nodes)
        assert plan["Plan"]["Actual Rows"] == 100

import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Memory, User
from services.memory import MemoryManager


def _session_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    import core.database as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", session_factory)
    return session_factory


def _add_user(session_factory, username):
    db = session_factory()
    try:
        db.add(User(
            public_id=f"user_{username}",
            username=username,
            password_hash="hash",
            is_admin=False,
            privileges={},
        ))
        db.commit()
    finally:
        db.close()


def test_memory_manager_imports_legacy_json_to_database(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path, monkeypatch)
    _add_user(session_factory, "alice")

    manager = MemoryManager(str(tmp_path))
    (tmp_path / "memory.json").write_text(json.dumps([
        {
            "id": "m1",
            "text": "Alice likes tea",
            "timestamp": 100,
            "source": "user",
            "category": "preference",
            "owner": "alice",
        }
    ]), encoding="utf-8")

    assert manager.load(owner="alice")[0]["text"] == "Alice likes tea"

    db = session_factory()
    try:
        row = db.query(Memory).filter(Memory.id == "m1").one()
        user = db.query(User).filter(User.username == "alice").one()
        assert row.owner_id == user.id
        assert row.category == "preference"
    finally:
        db.close()


def test_memory_manager_save_overwrites_database_rows(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path, monkeypatch)
    _add_user(session_factory, "bob")

    manager = MemoryManager(str(tmp_path))
    manager.save([
        {
            "id": "keep",
            "text": "Bob likes coffee",
            "timestamp": 100,
            "source": "user",
            "category": "preference",
            "owner": "bob",
        },
        {
            "id": "drop",
            "text": "Remove me",
            "timestamp": 101,
            "source": "user",
            "category": "fact",
            "owner": "bob",
        },
    ])

    manager.save([
        {
            "id": "keep",
            "text": "Bob likes espresso",
            "timestamp": 200,
            "source": "user",
            "category": "preference",
            "owner": "bob",
        }
    ])

    assert manager.load(owner="bob") == [{
        "id": "keep",
        "text": "Bob likes espresso",
        "timestamp": 200,
        "source": "user",
        "category": "preference",
        "owner": "bob",
    }]

    db = session_factory()
    try:
        assert db.query(Memory).count() == 1
        assert db.query(Memory).filter(Memory.id == "drop").count() == 0
    finally:
        db.close()

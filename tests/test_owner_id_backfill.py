import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.auth import AuthManager, _hash_password
from core.database import Base, Session as ChatSession, User, backfill_owner_ids


def test_owner_id_backfill_adds_columns_and_maps_legacy_owner(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE
            )
        """))
        conn.execute(text("INSERT INTO users (username) VALUES ('alice')"))
        conn.execute(text("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                endpoint_url TEXT NOT NULL,
                model TEXT NOT NULL,
                owner TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO sessions (id, name, endpoint_url, model, owner)
            VALUES ('s1', 'Chat', 'http://localhost', 'llm', 'alice')
        """))

    backfill_owner_ids(engine)
    backfill_owner_ids(engine)

    with engine.connect() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(sessions)")).fetchall()]
        assert columns.count("owner_id") == 1
        owner_id = conn.execute(text("SELECT owner_id FROM sessions WHERE id = 's1'")).scalar_one()
        alice_id = conn.execute(text("SELECT id FROM users WHERE username = 'alice'")).scalar_one()
        assert owner_id == alice_id


def test_auth_import_backfills_owner_ids_on_first_boot(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = session_factory()
    try:
        db.add(ChatSession(
            id="legacy-session",
            name="Legacy",
            endpoint_url="http://localhost",
            model="llm",
            owner="admin",
        ))
        db.commit()
    finally:
        db.close()

    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "users": {
            "admin": {
                "password_hash": _hash_password("temporary-password"),
                "role": "admin",
            }
        }
    }), encoding="utf-8")

    AuthManager(str(auth_path), session_factory=session_factory)

    db = session_factory()
    try:
        user = db.query(User).filter(User.username == "admin").one()
        session = db.query(ChatSession).filter(ChatSession.id == "legacy-session").one()
        assert session.owner_id == user.id
    finally:
        db.close()

import json
import os
import time

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.auth import AuthManager, _hash_password
from core.database import Base, User, UserSession


def _auth_manager(tmp_path, auth_data=None, sessions_data=None):
    auth_path = tmp_path / "auth.json"
    if auth_data is not None:
        auth_path.write_text(json.dumps(auth_data), encoding="utf-8")
    if sessions_data is not None:
        (tmp_path / "sessions.json").write_text(json.dumps(sessions_data), encoding="utf-8")

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return AuthManager(str(auth_path), session_factory=session_factory), session_factory


def test_auth_manager_imports_legacy_users_and_sessions(tmp_path):
    token = "legacy-token"
    mgr, session_factory = _auth_manager(
        tmp_path,
        auth_data={
            "signup_enabled": True,
            "users": {
                "AdminUser": {
                    "password_hash": _hash_password("temporary-password"),
                    "role": "admin",
                }
            },
        },
        sessions_data={
            token: {
                "username": "adminuser",
                "expiry": time.time() + 3600,
            }
        },
    )

    assert mgr.is_configured is True
    assert mgr.is_admin("adminuser") is True
    assert mgr.signup_enabled is True
    assert mgr.verify_password("adminuser", "temporary-password") is True
    assert mgr.validate_token(token) is True
    assert mgr.get_username_for_token(token) == "adminuser"

    db = session_factory()
    try:
        assert db.query(User).filter(User.username == "adminuser").count() == 1
        assert db.query(UserSession).count() == 1
    finally:
        db.close()


def test_session_tracks_user_id_across_rename(tmp_path):
    mgr, _session_factory = _auth_manager(tmp_path)

    assert mgr.setup("admin", "temporary-password")
    assert mgr.create_user("alice", "alice-password")
    token = mgr.create_session("alice", "alice-password")

    assert token
    assert mgr.rename_user("alice", "callum", "admin")
    assert mgr.validate_token(token) is True
    assert mgr.get_username_for_token(token) == "callum"


def test_signup_setting_is_database_backed(tmp_path):
    mgr, session_factory = _auth_manager(tmp_path)

    assert mgr.signup_enabled is False
    mgr.signup_enabled = True

    reloaded = AuthManager(str(tmp_path / "auth.json"), session_factory=session_factory)
    assert reloaded.signup_enabled is True


def test_delete_user_revokes_database_sessions(tmp_path):
    mgr, _session_factory = _auth_manager(tmp_path)

    assert mgr.setup("admin", "temporary-password")
    assert mgr.create_user("alice", "alice-password")
    token = mgr.create_session("alice", "alice-password")

    assert mgr.validate_token(token) is True
    assert mgr.delete_user("alice", "admin") is True
    assert mgr.validate_token(token) is False

import io
import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, StoredFile, User
from src.upload_handler import UploadHandler


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


def test_upload_handler_imports_legacy_uploads_json(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path, monkeypatch)
    _add_user(session_factory, "alice")

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    upload_id = "0123456789abcdef0123456789abcdef.txt"
    file_path = upload_dir / upload_id
    file_path.write_text("hello", encoding="utf-8")
    (upload_dir / "uploads.json").write_text(json.dumps({
        "alice:hash1": {
            "id": upload_id,
            "path": str(file_path),
            "mime": "text/plain",
            "size": 5,
            "name": "hello.txt",
            "hash": "hash1",
            "original_name": "hello.txt",
            "uploaded_at": "2026-01-01T00:00:00",
            "last_accessed": "2026-01-01T00:00:00",
            "owner": "alice",
        }
    }), encoding="utf-8")

    handler = UploadHandler(str(tmp_path), str(upload_dir))
    info = handler.get_upload_info(upload_id)

    assert info["id"] == upload_id
    assert info["owner"] == "alice"

    db = session_factory()
    try:
        row = db.query(StoredFile).filter(StoredFile.public_id == upload_id).one()
        user = db.query(User).filter(User.username == "alice").one()
        assert row.owner_id == user.id
        assert row.storage_path == str(file_path)
    finally:
        db.close()


def test_save_upload_persists_metadata_in_database_and_dedupes_by_owner(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path, monkeypatch)
    _add_user(session_factory, "bob")

    upload_dir = tmp_path / "uploads"
    handler = UploadHandler(str(tmp_path), str(upload_dir))

    first = handler.save_upload(
        UploadFile(filename="first.txt", file=io.BytesIO(b"same bytes")),
        client_ip="127.0.0.1",
        owner="bob",
    )
    duplicate = handler.save_upload(
        UploadFile(filename="second.txt", file=io.BytesIO(b"same bytes")),
        client_ip="127.0.0.1",
        owner="bob",
    )

    assert duplicate["is_duplicate"] is True
    assert duplicate["id"] == first["id"]
    assert not (upload_dir / "uploads.json").exists()

    db = session_factory()
    try:
        rows = db.query(StoredFile).all()
        user = db.query(User).filter(User.username == "bob").one()
        assert len(rows) == 1
        assert rows[0].owner_id == user.id
        assert rows[0].sha256 == first["hash"]
    finally:
        db.close()

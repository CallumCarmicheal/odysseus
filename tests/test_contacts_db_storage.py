import importlib
import json
import sys


def _import_contacts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    sys.modules.pop("core.database", None)
    sys.modules.pop("routes.contacts_routes", None)
    module = importlib.import_module("routes.contacts_routes")
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "LOCAL_CONTACTS_FILE", tmp_path / "contacts.json")
    module._contact_cache["contacts"] = []
    module._contact_cache["fetched_at"] = None
    return module


def test_local_contacts_import_legacy_json_to_database(tmp_path, monkeypatch):
    (tmp_path / "contacts.json").write_text(
        json.dumps({
            "contacts": [
                {
                    "uid": "contact1",
                    "name": "Alice Example",
                    "emails": ["alice@example.com"],
                    "phones": ["123"],
                }
            ]
        }),
        encoding="utf-8",
    )
    contacts = _import_contacts(tmp_path, monkeypatch)

    loaded = contacts._load_local_contacts()

    assert loaded == [{
        "uid": "contact1",
        "name": "Alice Example",
        "emails": ["alice@example.com"],
        "phones": ["123"],
    }]

    from core.database import LocalContact, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(LocalContact).filter(LocalContact.uid == "contact1").one()
        assert row.emails == ["alice@example.com"]
    finally:
        db.close()


def test_local_contacts_save_uses_database(tmp_path, monkeypatch):
    contacts = _import_contacts(tmp_path, monkeypatch)

    contacts._save_local_contacts([
        {
            "uid": "contact2",
            "name": "Bob Example",
            "emails": ["bob@example.com"],
            "phones": [],
        }
    ])

    assert not (tmp_path / "contacts.json").exists()
    assert contacts._load_local_contacts()[0]["name"] == "Bob Example"

    from core.database import LocalContact, SessionLocal

    db = SessionLocal()
    try:
        assert db.query(LocalContact).count() == 1
    finally:
        db.close()

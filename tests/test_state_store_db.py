import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import AppSetting, Base, FeatureFlag, User, UserPreference
import core.state_store as state_store
import src.settings as settings_mod


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _use_store_paths(monkeypatch, tmp_path, session_factory):
    monkeypatch.setattr(state_store, "_session", session_factory)
    monkeypatch.setattr(state_store, "PREFS_FILE", tmp_path / "user_prefs.json")
    monkeypatch.setattr(state_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(state_store, "FEATURES_FILE", tmp_path / "features.json")
    settings_mod._invalidate_caches()


def test_user_preferences_import_legacy_json_and_follow_rename(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    _use_store_paths(monkeypatch, tmp_path, session_factory)

    (tmp_path / "user_prefs.json").write_text(json.dumps({
        "_users": {
            "alice": {
                "theme": "dark",
                "default_model": "llama",
            }
        }
    }), encoding="utf-8")

    db = session_factory()
    try:
        db.add(User(
            public_id="user_alice",
            username="alice",
            password_hash="hash",
            is_admin=True,
            privileges={},
        ))
        db.commit()
    finally:
        db.close()

    assert state_store.load_user_preferences("alice")["theme"] == "dark"

    db = session_factory()
    try:
        pref = db.query(UserPreference).filter(UserPreference.owner_key == "alice").one()
        user = db.query(User).filter(User.username == "alice").one()
        assert pref.user_id == user.id
        user.username = "callum"
        db.commit()
    finally:
        db.close()

    assert state_store.load_user_preferences("callum")["default_model"] == "llama"

    db = session_factory()
    try:
        assert db.query(UserPreference).filter(UserPreference.owner_key == "callum").count() == 1
    finally:
        db.close()


def test_settings_and_features_import_json_then_save_to_database(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    _use_store_paths(monkeypatch, tmp_path, session_factory)

    (tmp_path / "settings.json").write_text(json.dumps({
        "image_quality": "high",
        "legacy_only": "remove-me",
    }), encoding="utf-8")
    (tmp_path / "features.json").write_text(json.dumps({
        "memory": False,
        "legacy_feature": True,
    }), encoding="utf-8")

    assert settings_mod.load_settings()["image_quality"] == "high"
    assert settings_mod.load_features()["memory"] is False

    settings_mod.save_settings({"image_quality": "low"})
    settings_mod.save_features({"memory": True})

    db = session_factory()
    try:
        assert db.query(AppSetting).filter(AppSetting.key == "image_quality").one().value == "low"
        assert db.query(AppSetting).filter(AppSetting.key == "legacy_only").count() == 0
        assert db.query(FeatureFlag).filter(FeatureFlag.key == "memory").one().enabled is True
        assert db.query(FeatureFlag).filter(FeatureFlag.key == "legacy_feature").count() == 0
    finally:
        db.close()

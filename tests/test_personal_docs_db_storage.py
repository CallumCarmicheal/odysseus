import json
import sys


def _fresh_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    sys.modules.pop("core.database", None)
    from src.personal_docs import PersonalDocsManager

    return PersonalDocsManager(str(tmp_path), rag_manager=None)


def _stored_personal_docs_state():
    from core.database import AppSetting, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "personal_docs_state").one()
        return dict(row.value or {})
    finally:
        db.close()


def test_personal_docs_imports_legacy_tracking_sidecars(tmp_path, monkeypatch):
    docs_dir = tmp_path
    extra_dir = tmp_path / "external"
    extra_dir.mkdir()
    excluded = tmp_path / "secret.txt"
    excluded.write_text("secret", encoding="utf-8")
    (docs_dir / "indexed_directories.json").write_text(
        json.dumps([str(extra_dir)]),
        encoding="utf-8",
    )
    (docs_dir / "excluded_files.json").write_text(
        json.dumps([str(excluded)]),
        encoding="utf-8",
    )

    manager = _fresh_manager(tmp_path, monkeypatch)

    assert manager.get_indexed_directories() == [str(extra_dir)]
    assert manager.excluded_files == {str(excluded)}
    assert _stored_personal_docs_state() == {
        "indexed_directories": [str(extra_dir)],
        "excluded_files": [str(excluded)],
    }


def test_personal_docs_save_uses_database(tmp_path, monkeypatch):
    manager = _fresh_manager(tmp_path, monkeypatch)
    extra_dir = tmp_path / "external"
    extra_dir.mkdir()
    excluded = tmp_path / "ignored.md"
    excluded.write_text("ignore", encoding="utf-8")

    manager.add_directory(str(extra_dir), index=False)
    manager.exclude_file(str(excluded))

    assert not (tmp_path / "indexed_directories.json").exists()
    assert not (tmp_path / "excluded_files.json").exists()

    reloaded = _fresh_manager(tmp_path, monkeypatch)
    assert reloaded.get_indexed_directories() == [str(extra_dir)]
    assert reloaded.excluded_files == {str(excluded)}

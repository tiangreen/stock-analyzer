import pytest

from stock_analyzer import config, db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.reset_for_tests()
    yield
    db.reset_for_tests()

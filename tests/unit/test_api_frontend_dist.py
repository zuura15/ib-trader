"""ib-api serving the built SPA: index.html must never be cached."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import ib_trader.data.models  # noqa: F401  (register models)
from ib_trader.api import app as app_module
from ib_trader.data.models import Base


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/index-abc.js"></script>')
    (dist / "assets" / "index-abc.js").write_text("console.log(1)")
    (dist / "favicon.ico").write_bytes(b"\x00")
    monkeypatch.setattr(app_module, "_FRONTEND_DIST", dist)
    engine = create_engine(f"sqlite:///{tmp_path / 'db.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    app = app_module.create_app(scoped_session(sessionmaker(bind=engine)), api_key="k" * 40)
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/index.html", "/some/client/route"])
def test_index_html_is_no_cache(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "index-abc.js" in resp.text
    assert resp.headers["cache-control"] == "no-cache"


def test_hashed_assets_and_root_files_keep_default_caching(client):
    assert "cache-control" not in client.get("/assets/index-abc.js").headers
    assert "cache-control" not in client.get("/favicon.ico").headers


def test_spa_shell_is_public_but_api_is_not(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 401

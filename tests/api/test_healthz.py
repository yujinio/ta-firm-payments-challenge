from sqlalchemy.exc import OperationalError

from app.db import get_session
from app.main import app


def test_healthz_reports_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


class _UnreachableDatabase:
    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_healthz_reports_unavailable(client):
    # /healthz is for the balancer and Compose, so it keeps its own body (spec section 8.1).
    app.dependency_overrides[get_session] = _UnreachableDatabase
    try:
        r = client.get("/healthz")
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable"}

from fastapi.testclient import TestClient

from classroom_sync.app import api

client = TestClient(api)


def test_health_answers():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

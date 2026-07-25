"""
Tests for the unauthenticated health check endpoints.

Tests cover:
- GET /health - liveness probe used by the compose healthcheck
- GET /db - database health with connection pool status
"""

from starlette.testclient import TestClient


class TestHealthcheck:
    """Tests for GET /health."""

    def test_health_returns_ok(self, client: TestClient) -> None:
        """Health endpoint should return 200 with an ok status."""
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestDatabaseHealth:
    """Tests for GET /db."""

    def test_db_health_returns_pool_status(self, client: TestClient) -> None:
        """Database health endpoint should return 200 with pool status."""
        response = client.get("/db")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "pool" in data

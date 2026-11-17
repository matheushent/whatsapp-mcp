"""Tests for bearer auth on the network MCP transports."""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_auth import (
    BearerAuthMiddleware,
    build_authenticated_app,
    check_bearer,
    is_loopback_host,
    require_network_auth_token,
    resolve_auth_token,
)

TOKEN = "t" * 32


class TestResolveAuthToken:
    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_unset_means_no_token(self, value):
        assert resolve_auth_token(value) is None

    def test_strips_whitespace(self):
        assert resolve_auth_token(f"  {TOKEN} ") == TOKEN

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            resolve_auth_token("short")


class TestRequireNetworkAuthToken:
    def test_stdio_needs_no_token(self):
        assert require_network_auth_token("stdio", None) is None

    @pytest.mark.parametrize("transport", ["streamable-http", "sse"])
    def test_network_transport_fails_closed_without_token(self, transport):
        with pytest.raises(ValueError, match="WHATSAPP_MCP_AUTH_TOKEN must be set"):
            require_network_auth_token(transport, None)

    @pytest.mark.parametrize("transport", ["streamable-http", "sse"])
    def test_network_transport_returns_token(self, transport):
        assert require_network_auth_token(transport, TOKEN) == TOKEN


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"])
    def test_loopback(self, host):
        assert is_loopback_host(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", ""])
    def test_non_loopback(self, host):
        assert not is_loopback_host(host)


class TestCheckBearer:
    def test_accepts_exact_token(self):
        assert check_bearer(f"Bearer {TOKEN}", TOKEN)

    @pytest.mark.parametrize(
        "header",
        [None, "", TOKEN, f"Basic {TOKEN}", f"bearer {TOKEN}", "Bearer ", f"Bearer {TOKEN}x", f"Bearer x{TOKEN}"],
    )
    def test_rejects_everything_else(self, header):
        assert not check_bearer(header, TOKEN)

    def test_non_ascii_header_does_not_raise(self):
        assert not check_bearer("Bearer tøken", TOKEN)


def _app() -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(BearerAuthMiddleware, token=TOKEN)
    return app


class TestBearerAuthMiddleware:
    def test_missing_token_is_401_with_challenge(self):
        response = TestClient(_app()).get("/")
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert "ok" not in response.text

    def test_wrong_token_is_401(self):
        assert TestClient(_app()).get("/", headers={"Authorization": "Bearer wrong-token-value"}).status_code == 401

    def test_correct_token_passes(self):
        response = TestClient(_app()).get("/", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        assert response.text == "ok"


class TestBuildAuthenticatedApp:
    @pytest.mark.parametrize(("transport", "path"), [("streamable-http", "/mcp"), ("sse", "/sse")])
    def test_real_fastmcp_routes_reject_unauthenticated(self, transport, path):
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("test")
        client = TestClient(build_authenticated_app(server, transport, TOKEN))
        assert client.post(path, json={}).status_code == 401
        assert client.get(path).status_code == 401
        assert client.post(path, json={}, headers={"Authorization": "Bearer nope-nope-nope-nope"}).status_code == 401

    def test_unknown_transport_raises(self):
        from mcp.server.fastmcp import FastMCP

        with pytest.raises(ValueError):
            build_authenticated_app(FastMCP("test"), "stdio", TOKEN)

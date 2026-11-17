"""Bearer-token authentication for the network (http/sse) MCP transports.

The stdio transport is a private pipe to the launching client, so it needs no
auth. The http and sse transports open a TCP listener that exposes every tool
(read all messages, send as the user), and FastMCP ships no authentication for
them. Without this layer any local process, or any host that can reach a
non-loopback bind, could drive the user's WhatsApp account.

Side-effect-free: importing this module never reads env vars or opens sockets.
"""

import hmac
import ipaddress
from typing import Any

AUTH_TOKEN_ENV = "WHATSAPP_MCP_AUTH_TOKEN"
MIN_TOKEN_LENGTH = 16


def resolve_auth_token(value: str | None) -> str | None:
    """Parse WHATSAPP_MCP_AUTH_TOKEN. Unset or whitespace-only means no token.

    Raises ValueError for a token shorter than MIN_TOKEN_LENGTH.
    """
    token = (value or "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"{AUTH_TOKEN_ENV} is too short (need at least {MIN_TOKEN_LENGTH} chars)")
    return token


def require_network_auth_token(transport: str, value: str | None) -> str | None:
    """Return the auth token a transport must enforce, failing closed.

    stdio returns None (no listener). http/sse raise ValueError when no token is
    configured, so the server never starts an unauthenticated network listener.
    """
    if transport == "stdio":
        return None
    token = resolve_auth_token(value)
    if token is None:
        raise ValueError(
            f"{AUTH_TOKEN_ENV} must be set for the {transport} transport: the server can read and send "
            "WhatsApp messages on your account and must not listen unauthenticated. "
            'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return token


def is_loopback_host(host: str) -> bool:
    """True when a bind address only accepts connections from this machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_bearer(auth_header: str | None, expected: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer <token>`` header."""
    prefix = "Bearer "
    if not auth_header or not auth_header.startswith(prefix):
        return False
    got = auth_header[len(prefix) :].strip()
    return hmac.compare_digest(got.encode(), expected.encode())


class BearerAuthMiddleware:
    """Pure-ASGI middleware that rejects requests without the bearer token.

    Pure ASGI (not BaseHTTPMiddleware) so streaming responses (SSE, streamable
    HTTP) are passed through untouched. Lifespan events are not authenticated.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if check_bearer(auth, self.token):
                await self.app(scope, receive, send)
                return
            body = b"Unauthorized"
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="whatsapp-mcp"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        # Any other scope type (e.g. websocket) is not served by these transports: deny.
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})


def build_authenticated_app(server: Any, transport: str, token: str) -> Any:
    """Build the FastMCP Starlette app for a network transport behind bearer auth."""
    if transport == "streamable-http":
        app = server.streamable_http_app()
    elif transport == "sse":
        app = server.sse_app()
    else:
        raise ValueError(f"build_authenticated_app does not serve transport {transport!r}")
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app

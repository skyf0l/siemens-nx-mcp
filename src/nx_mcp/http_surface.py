"""Serve compatible and compact profiles against one serial NX bridge.

Binds 127.0.0.1:8765 by default. Host and port come from arguments or
NX_MCP_HTTP_HOST / NX_MCP_HTTP_PORT. The HTTP endpoint has no authentication of
its own, so a non-loopback address is refused unless explicitly allowed with
--allow-remote or NX_MCP_HTTP_ALLOW_REMOTE=1.
"""

import os
from contextlib import AsyncExitStack, asynccontextmanager

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from nx_mcp.server import create_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def resolve_bind(host=None, port=None, allow_remote=None):
    """Return (host, port) from arguments, then environment, then loopback defaults."""
    host = host or os.environ.get("NX_MCP_HTTP_HOST") or DEFAULT_HOST
    port = int(port or os.environ.get("NX_MCP_HTTP_PORT") or DEFAULT_PORT)
    if allow_remote is None:
        allow_remote = os.environ.get("NX_MCP_HTTP_ALLOW_REMOTE", "").lower() in {"1", "true"}
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise ValueError(
            f"Refusing to serve the unauthenticated NX MCP HTTP endpoint on {host}; use a "
            "loopback host or opt in with --allow-remote / NX_MCP_HTTP_ALLOW_REMOTE=1 behind "
            "your own network access controls"
        )
    return host, port


def create_app(bridge, workspace=None, host=DEFAULT_HOST, port=DEFAULT_PORT, allow_remote=False):
    resolve_bind(host, port, allow_remote)
    servers = [create_server(bridge, workspace, surface=profile) for profile in ("full", "agent")]
    addresses = dict.fromkeys((host, "127.0.0.1", "localhost"))
    for server in servers:
        server.settings.streamable_http_path = "/mcp"
        server.settings.json_response = True
        server.settings.stateless_http = True
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{address}:{port}" for address in addresses],
            allowed_origins=[f"http://{address}:{port}" for address in addresses],
        )
    apps = [server.streamable_http_app() for server in servers]

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(
        routes=[Mount("/agent", app=apps[1]), Mount("/", app=apps[0])], lifespan=lifespan
    )


def main(argv=None):
    import argparse
    from pathlib import Path

    import uvicorn

    from nx_mcp.bridge import DescriptorBridgeClient

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", help="bind address (default NX_MCP_HTTP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default NX_MCP_HTTP_PORT or 8765)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        default=None,
        help="allow a non-loopback bind address (the endpoint is unauthenticated)",
    )
    args = parser.parse_args(argv)
    host, port = resolve_bind(args.host, args.port, args.allow_remote)
    descriptor = Path(
        os.environ.get(
            "NX_MCP_BRIDGE_DESCRIPTOR",
            str(Path(os.environ["LOCALAPPDATA"]) / "nx-mcp" / "bridge.json"),
        )
    )
    app = create_app(DescriptorBridgeClient(descriptor), host=host, port=port, allow_remote=True)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

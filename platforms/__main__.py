"""Command line.

    python -m platforms init                  create both schemas and their tables
    python -m platforms seed [--reset]        load the customers, plans and coverages
    python -m platforms api a|b|directory          one REST API (a platform's, or the plan directory's)
    python -m platforms mcp a|b|directory [--stdio] its MCP server (streamable HTTP by default)
    python -m platforms web a|b                    a platform's administration screens (no sign-in)
    python -m platforms serve                      every server above, until Ctrl+C
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time

from platforms.config import PLATFORMS, platform


def _api(key: str) -> None:
    import uvicorn

    if key == "directory":
        from platforms.config import DIRECTORY
        from platforms.directory import create_directory_app

        uvicorn.run(create_directory_app(), host="127.0.0.1", port=DIRECTORY.api_port, log_level="info", server_header=False)
        return
    from platforms.api import create_app

    p = platform(key)
    uvicorn.run(create_app(p), host="127.0.0.1", port=p.api_port, log_level="info", server_header=False)


def _mcp(key: str, stdio: bool) -> None:
    if key == "directory":
        from platforms.config import DIRECTORY
        from platforms.directory_mcp import build_directory_server

        if not DIRECTORY.api_key:
            raise SystemExit(f"{DIRECTORY.api_key_env} is not set; the MCP server needs it to call the directory API")
        server = build_directory_server()
        if stdio:
            server.run("stdio")
            return
        print(f"{DIRECTORY.name} MCP server: http://127.0.0.1:{DIRECTORY.mcp_port}/mcp -> {DIRECTORY.api_base_url}", flush=True)
        server.run("streamable-http")
        return
    from platforms.mcp_server import build_server

    p = platform(key)
    if not p.api_key:
        raise SystemExit(f"{p.api_key_env} is not set; the MCP server needs it to call the {p.name} API")
    server = build_server(p)
    if stdio:
        server.run("stdio")
        return
    print(f"{p.name} MCP server: http://127.0.0.1:{p.mcp_port}/mcp (streamable HTTP) -> {p.api_base_url}", flush=True)
    server.run("streamable-http")


def _web(key: str) -> None:
    import uvicorn

    from platforms.web import create_web_app

    p = platform(key)
    print(f"{p.name} screens: http://127.0.0.1:{p.web_port}/ (no sign-in; test use only)", flush=True)
    uvicorn.run(create_web_app(p), host="127.0.0.1", port=p.web_port, log_level="info", server_header=False)


def _serve() -> None:
    """Every server as a child process -- both platforms' APIs, MCP servers
    and screens, and the directory's API and MCP server -- so one dying is
    visible and the rest stop with it rather than serving half a system."""
    from platforms.config import DIRECTORY

    keys = [*PLATFORMS, "directory"]
    commands = [[sys.executable, "-m", "platforms", kind, key] for kind in ("api", "mcp") for key in keys]
    commands += [[sys.executable, "-m", "platforms", "web", key] for key in PLATFORMS]
    children = [subprocess.Popen(c) for c in commands]
    for p in [*PLATFORMS.values(), DIRECTORY]:
        screens = f"   screens http://127.0.0.1:{p.web_port}/" if getattr(p, "web_port", 0) else ""
        print(f"{p.name}: API http://127.0.0.1:{p.api_port}/docs   MCP http://127.0.0.1:{p.mcp_port}/mcp{screens}", flush=True)
    try:
        while all(c.poll() is None for c in children):
            time.sleep(0.5)
        failed = [" ".join(cmd[2:]) for cmd, c in zip(commands, children) if c.poll() is not None]
        print(f"stopped: {', '.join(failed)} exited; stopping the rest", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for c in children:
            if c.poll() is None:
                c.terminate()
        for c in children:
            try:
                c.wait(timeout=10)
            except subprocess.TimeoutExpired:
                c.kill()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m platforms")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create both schemas and their tables")
    seed = sub.add_parser("seed", help="load the customers, plans and coverages")
    seed.add_argument("--reset", action="store_true", help="drop both schemas first (deletes every change made since)")
    api = sub.add_parser("api", help="run one platform's REST API")
    api.add_argument("platform", choices=[*sorted(PLATFORMS), "directory"])
    mcp = sub.add_parser("mcp", help="run one platform's MCP server")
    mcp.add_argument("platform", choices=[*sorted(PLATFORMS), "directory"])
    mcp.add_argument("--stdio", action="store_true", help="serve over stdio instead of streamable HTTP")
    web = sub.add_parser("web", help="run one platform's administration screens (no sign-in)")
    web.add_argument("platform", choices=sorted(PLATFORMS))
    sub.add_parser("serve", help="run every server: both platforms' APIs, MCP servers and screens, and the directory's")
    args = parser.parse_args(argv)

    if args.command == "init":
        from platforms.db import create_schema

        for p in PLATFORMS.values():
            create_schema(p)
            print(f"{p.schema}: ready")
    elif args.command == "seed":
        from platforms.seed import seed as run_seed

        for p in PLATFORMS.values():
            counts = run_seed(p, reset=args.reset)
            print(f"{p.schema}: {counts['customers']} customers, {counts['plans']} plans, {counts['coverages']} coverages")
    elif args.command == "api":
        _api(args.platform)
    elif args.command == "mcp":
        _mcp(args.platform, args.stdio)
    elif args.command == "web":
        _web(args.platform)
    else:
        _serve()


if __name__ == "__main__":
    main()

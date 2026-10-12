"""The real entry point over stdio and streamable HTTP, on the bundled demo data."""
import json
import os
import socket
import subprocess
import sys
import time

import anyio
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from prozorro_mcp.demo import DEMO_FILE, demo_store

pytestmark = pytest.mark.skipif(not DEMO_FILE.exists(), reason="demo snapshot not built")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    old = os.environ.get("PROZORRO_MCP_CACHE")
    os.environ["PROZORRO_MCP_CACHE"] = str(cache)
    demo_store().close()  # build once so the subprocesses start fast
    yield {**os.environ, "PROZORRO_MCP_CACHE": str(cache), "PYTHONIOENCODING": "utf-8"}
    if old is None:
        os.environ.pop("PROZORRO_MCP_CACHE")
    else:
        os.environ["PROZORRO_MCP_CACHE"] = old


def payload(result):
    sc = getattr(result, "structured_content", None)
    return sc if sc else json.loads(result.content[0].text)


def test_stdio_serves_the_demo(env):
    params = StdioServerParameters(command=sys.executable, args=["-m", "prozorro_mcp", "serve", "--demo"], env=env)

    async def go():
        async with Client(params) as client:
            return await client.call_tool("search_tenders", {"status": "any", "limit": 3})
    res = anyio.run(go)
    assert not res.is_error and payload(res)["total"] > 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_serves_the_demo(env):
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "prozorro_mcp", "serve", "--demo", "--http", "--port",
                             str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)

        async def go():
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                return await client.call_tool("coverage", {})
        res = anyio.run(go)
        assert not res.is_error and payload(res)["tenders"] > 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)

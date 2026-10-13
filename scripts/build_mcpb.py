"""Build the Claude Desktop extension (dist/prozorro-mcp.mcpb, a stable name for releases/latest/download).

The bundle is tiny: a manifest, a pyproject that depends on prozorro-mcp from PyPI and a one-line entry
point. Claude Desktop installs Python and the package with uv. The tool list and the version are taken from
the code, so the manifest cannot drift from the server.

    python scripts/build_mcpb.py                      # needs Node (npx) for the official packer
    python scripts/build_mcpb.py --local-wheel dist/prozorro_mcp-0.1.0-py3-none-any.whl   # test before PyPI
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import anyio
from mcp.client import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prozorro_mcp.server import build_server
from prozorro_mcp.store import Store


class _NoSync:
    def start(self, *a, **kw):
        return {}

    def status(self):
        return {}


def tools() -> list[dict]:
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            try:
                async with Client(build_server(store, _NoSync())) as client:
                    return (await client.list_tools()).tools
            finally:
                store.close()
    return [{"name": t.name, "description": t.description.split(". ")[0].rstrip(".") + "."}
            for t in sorted(anyio.run(go), key=lambda t: t.name)]


def manifest(version: str) -> dict:
    return {
        "manifest_version": "0.4",
        "name": "prozorro-mcp",
        "display_name": "Prozorro",
        "version": version,
        "description": "Ukrainian public procurement (Prozorro): open tenders, upcoming plans, market, "
                       "competitors and red flags from a local copy of the official open data.",
        "long_description": (
            "Ask about Prozorro in plain words: open tenders in your oblast, what buyers plan to purchase, "
            "who wins and at what price, your competitors, red flags. Every number is computed by code from "
            "the official data.\n\nYour regions download in the background (leave the settings empty and "
            "Claude asks which oblast and period you need); ask Claude to sync more regions any time. No "
            "keys or accounts. Data is kept in ~/.prozorro-mcp and is shared with the prozorro-mcp CLI."),
        "author": {"name": "Kyrylo Kalashnyk", "url": "https://github.com/kotiniiqq"},
        "repository": {"type": "git", "url": "https://github.com/kotiniiqq/prozorro-mcp"},
        "homepage": "https://github.com/kotiniiqq/prozorro-mcp",
        "documentation": "https://github.com/kotiniiqq/prozorro-mcp/blob/main/docs/guide.uk.md",
        "support": "https://github.com/kotiniiqq/prozorro-mcp/issues",
        "icon": "icon.png",
        "server": {
            "type": "uv",
            "entry_point": "src/server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "src/server.py", "serve", "--sync-on-start",
                         "--region", "${user_config.regions}", "--days", "${user_config.days}"],
            },
        },
        "tools": tools(),
        "keywords": ["prozorro", "ukraine", "procurement", "tenders", "open-data"],
        "license": "MIT",
        "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.11"}},
        "user_config": {
            "regions": {
                "type": "string",
                "title": "Області / Regions",
                "description": "Які області завантажувати, через кому: «Львівська, Волинська», «Київ». "
                               "Можна лишити порожнім і попросити Claude пізніше. / Comma-separated oblasts.",
                "default": "",
                "required": False,
            },
            "days": {
                "type": "number",
                "title": "Днів історії / Days of history",
                "description": "Скільки днів назад завантажувати. Тиждень великої області — близько 20 хвилин "
                               "і 350 МБ. / How many days back.",
                "default": 7,
                "min": 1,
                "max": 92,
                "required": False,
            },
        },
    }


def pyproject(version: str, local_wheel: Path | None) -> str:
    text = (f'[project]\nname = "prozorro-mcp-desktop"\nversion = "{version}"\nrequires-python = ">=3.11"\n'
            f'dependencies = ["prozorro-mcp=={version}"]\n')
    if local_wheel:
        text += f'\n[tool.uv.sources]\nprozorro-mcp = {{ path = "{local_wheel.resolve().as_posix()}" }}\n'
    return text


def build(local_wheel: Path | None = None) -> Path:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "bundle"
        shutil.copytree(ROOT / "mcpb", stage)
        (stage / "manifest.json").write_text(json.dumps(manifest(version), ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        (stage / "pyproject.toml").write_text(pyproject(version, local_wheel), encoding="utf-8")
        out = ROOT / "dist" / ("prozorro-mcp-local.mcpb" if local_wheel else "prozorro-mcp.mcpb")
        out.parent.mkdir(exist_ok=True)
        npx = shutil.which("npx") or "npx"
        subprocess.run([npx, "-y", "@anthropic-ai/mcpb@2", "validate", str(stage / "manifest.json")], check=True)
        subprocess.run([npx, "-y", "@anthropic-ai/mcpb@2", "pack", str(stage), str(out)], check=True)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local-wheel", type=Path, help="install this wheel instead of PyPI (testing only)")
    print(build(p.parse_args().local_wheel))

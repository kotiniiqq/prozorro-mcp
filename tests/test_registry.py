"""Files read by MCP catalogs must agree with the package: name, version, README marker."""
import json
import re
import tomllib
from pathlib import Path

from prozorro_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_versions_agree_everywhere():
    assert __version__ == VERSION == SERVER["version"] == SERVER["packages"][0]["version"]


def test_registry_entry_points_at_the_pypi_package_and_fits_the_limits():
    pkg = SERVER["packages"][0]
    assert SERVER["name"] == "io.github.kotiniiqq/prozorro-mcp"
    assert pkg["registryType"] == "pypi" and pkg["identifier"] == "prozorro-mcp"
    assert pkg["runtimeHint"] == "uvx" and pkg["transport"] == {"type": "stdio"}
    assert [a["value"] for a in pkg["packageArguments"]] == ["serve"]
    assert len(SERVER["description"]) <= 100


def test_readme_carries_the_ownership_marker_the_registry_checks_on_pypi():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"mcp-name: io\.github\.kotiniiqq/prozorro-mcp(\s|-->)", readme)


def test_glama_maintainer():
    assert json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))["maintainers"] == ["kotiniiqq"]

"""The Claude Desktop extension manifest is generated from the code; check what it promises."""
import importlib.util
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "scripts" / "build_mcpb.py")
build_mcpb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_mcpb)
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_manifest_lists_every_server_tool_and_the_package_version():
    m = build_mcpb.manifest(VERSION)
    from test_server import LOCAL_TOOLS, TOOLS
    assert sorted(t["name"] for t in m["tools"]) == sorted(TOOLS + LOCAL_TOOLS)
    assert m["version"] == VERSION and m["server"]["type"] == "uv"


def test_every_setting_used_in_the_command_is_declared():
    m = build_mcpb.manifest(VERSION)
    used = set(re.findall(r"\$\{user_config\.(\w+)\}", " ".join(m["server"]["mcp_config"]["args"])))
    assert used == set(m["user_config"]) == {"regions", "days"}


def test_bundle_pins_the_same_version_from_pypi_unless_testing_a_local_wheel():
    assert f'"prozorro-mcp=={VERSION}"' in build_mcpb.pyproject(VERSION, None)
    assert "tool.uv.sources" not in build_mcpb.pyproject(VERSION, None)
    assert "tool.uv.sources" in build_mcpb.pyproject(VERSION, ROOT / "x.whl")


def test_bundle_entry_point_and_icon_exist():
    assert (ROOT / "mcpb" / "src" / "server.py").exists() and (ROOT / "mcpb" / "icon.png").exists()

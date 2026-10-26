import json
import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest


def test_skill_installer_and_no_overwrite(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "install_skill.py"
    target = tmp_path / "fin"
    args = [sys.executable, str(script), "--target", str(target)]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["installed"] == str(target / "SKILL.md")
    content = (target / "SKILL.md").read_text()
    assert "name: fin" in content
    source = script.parents[1] / "skill" / "SKILL.md"
    assert content == source.read_text(encoding="utf-8")
    assert "fin MCP server" in content
    assert "fin_sync" in content
    assert "## Local Installation" not in content
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0
    assert (target / "SKILL.md").read_text() == content


def test_documented_mcp_examples_match_tool_schemas(tmp_path):
    fastmcp = pytest.importorskip("fastmcp")
    from jsonschema import validate
    from fin.config import Config
    from fin.mcp_server import create_server

    content = (Path(__file__).resolve().parents[1] / "skill" / "SKILL.md").read_text(encoding="utf-8")
    examples = re.findall(r"^- `(fin_\w+)` with `(\{[^\n]*?\})`:", content, re.MULTILINE)

    async def check():
        async with fastmcp.Client(create_server(Config(tmp_path / "unused.db"))) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert {name for name, _ in examples} == set(tools)
            assert len(examples) == 12
            for name, arguments in examples:
                validate(json.loads(arguments), tools[name].input_schema)
    asyncio.run(check())

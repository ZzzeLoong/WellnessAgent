"""protocols/mcp 软依赖单测（Phase 3.0 · P3.0-4）。

修复前：``protocols/mcp/server.py`` 在**模块顶层** ``raise ImportError``，而
``fastmcp`` 并不在 ``requirements.txt`` 的必装段里 —— 任何人只要 import 这个模块
（包括 pytest 收集、静态分析）就直接崩。同时 ``protocols/__init__.py`` 还硬 import
了本仓库并未随包引入的 ``.a2a`` / ``.anp``，让 ``import protocols.mcp.server``
连 ``__init__`` 都过不去。

修复后的契约：
1. 无 fastmcp 环境下 ``import protocols.mcp.server`` / ``import protocols.mcp``
   / ``import protocols`` 全部成功；
2. ``MCPServer(...)`` 实例化时才抛**友好** ``ImportError``（含安装提示）；
3. ``is_available()`` / ``FASTMCP_AVAILABLE`` 可供调用方提前探测；
4. 与 fastmcp 无关的工具函数（``create_context`` 等）始终可用。
"""

from __future__ import annotations

import importlib
import sys

import pytest

from WellnessAgent.protocols import mcp as mcp_pkg
from WellnessAgent.protocols.mcp import server as server_module


# ---------------------------------------------------------------- import 可用性
class TestImportNeverFails:
    def test_import_server_module(self):
        module = importlib.import_module("WellnessAgent.protocols.mcp.server")
        assert module is server_module

    def test_import_mcp_package(self):
        module = importlib.import_module("WellnessAgent.protocols.mcp")
        assert hasattr(module, "MCPServer")

    def test_import_protocols_package(self):
        """protocols/__init__ 不得因缺少 a2a/anp 子包而崩。"""
        module = importlib.import_module("WellnessAgent.protocols")
        assert hasattr(module, "Protocol")
        assert hasattr(module, "MCPServer")

    def test_reimport_is_stable(self):
        """重复 reload 不应引入副作用（顶层不再 raise）。"""
        module = importlib.reload(server_module)
        assert hasattr(module, "MCPServer")


# ---------------------------------------------------------------- 探测接口
class TestAvailabilityProbe:
    def test_flag_and_helper_agree(self):
        assert server_module.is_available() is server_module.FASTMCP_AVAILABLE

    def test_flag_is_boolean(self):
        assert isinstance(server_module.FASTMCP_AVAILABLE, bool)

    def test_package_reexports_flag(self):
        assert mcp_pkg.MCP_SERVER_AVAILABLE is server_module.FASTMCP_AVAILABLE

    def test_fastmcp_is_none_when_unavailable(self):
        if server_module.FASTMCP_AVAILABLE:
            pytest.skip("环境已安装 fastmcp")
        assert server_module.FastMCP is None

    def test_missing_message_mentions_install_hint(self):
        assert "pip install fastmcp" in server_module.FASTMCP_MISSING_MESSAGE


# ---------------------------------------------------------------- 延后报错
class TestFriendlyErrorAtConstruction:
    def test_require_fastmcp_raises_when_missing(self, monkeypatch):
        monkeypatch.setattr(server_module, "FASTMCP_AVAILABLE", False)
        with pytest.raises(ImportError) as excinfo:
            server_module.require_fastmcp()
        assert "fastmcp" in str(excinfo.value)

    def test_require_fastmcp_is_noop_when_present(self, monkeypatch):
        monkeypatch.setattr(server_module, "FASTMCP_AVAILABLE", True)
        assert server_module.require_fastmcp() is None

    def test_server_construction_raises_friendly_error(self, monkeypatch):
        monkeypatch.setattr(server_module, "FASTMCP_AVAILABLE", False)
        with pytest.raises(ImportError) as excinfo:
            server_module.MCPServer(name="wellness")
        message = str(excinfo.value)
        assert "fastmcp" in message
        assert "pip install fastmcp" in message

    def test_builder_construction_raises_friendly_error(self, monkeypatch):
        monkeypatch.setattr(server_module, "FASTMCP_AVAILABLE", False)
        with pytest.raises(ImportError):
            server_module.MCPServerBuilder("wellness")

    def test_example_server_raises_friendly_error(self, monkeypatch):
        monkeypatch.setattr(server_module, "FASTMCP_AVAILABLE", False)
        with pytest.raises(ImportError):
            server_module.create_example_server()


# ---------------------------------------------------------------- 装了 fastmcp 的路径
class TestConstructionWithFakeFastmcp:
    """用替身模拟「已安装 fastmcp」，验证正常路径不被软依赖改动破坏。"""

    @pytest.fixture()
    def fake_fastmcp(self, monkeypatch):
        class FakeFastMCP:
            def __init__(self, name: str):
                self.name = name
                self.tools: list[tuple[str | None, str | None]] = []

            def tool(self, name=None, description=None):
                def decorator(func):
                    self.tools.append((name, description))
                    return func

                return decorator

            def run(self, transport="stdio", **kwargs):
                self.ran = {"transport": transport, **kwargs}

        monkeypatch.setattr(server_module, "FASTMCP_AVAILABLE", True)
        monkeypatch.setattr(server_module, "FastMCP", FakeFastMCP)
        return FakeFastMCP

    def test_construct_and_get_info(self, fake_fastmcp):
        server = server_module.MCPServer(name="wellness", description="desc")
        assert server.get_info() == {
            "name": "wellness",
            "description": "desc",
            "protocol": "MCP",
        }

    def test_default_description(self, fake_fastmcp):
        server = server_module.MCPServer(name="wellness")
        assert server.description == "wellness MCP Server"

    def test_add_tool_registers(self, fake_fastmcp):
        server = server_module.MCPServer(name="wellness")
        server.add_tool(lambda x: x, name="t1", description="d1")
        assert server.mcp.tools == [("t1", "d1")]

    def test_example_server_registers_two_tools(self, fake_fastmcp):
        server = server_module.create_example_server()
        assert len(server.mcp.tools) == 2

    def test_builder_chain(self, fake_fastmcp):
        builder = server_module.MCPServerBuilder("wellness").with_tool(
            lambda x: x, name="t", description="d"
        )
        built = builder.build()
        assert built.mcp.tools == [("t", "d")]

    def test_run_passes_transport(self, fake_fastmcp):
        server = server_module.MCPServer(name="wellness")
        server.run(transport="http", port=8081)
        assert server.mcp.ran == {"transport": "http", "port": 8081}


# ---------------------------------------------------------------- 无依赖工具函数
class TestDependencyFreeUtils:
    def test_create_context_defaults(self):
        ctx = mcp_pkg.create_context()
        assert ctx == {"messages": [], "tools": [], "resources": [], "metadata": {}}

    def test_parse_context_fills_required_fields(self):
        parsed = mcp_pkg.parse_context('{"messages": [{"role": "user"}]}')
        assert parsed["messages"] == [{"role": "user"}]
        assert parsed["tools"] == []
        assert parsed["metadata"] == {}

    def test_parse_context_rejects_bad_json(self):
        with pytest.raises(ValueError):
            mcp_pkg.parse_context("{not json")


# ---------------------------------------------------------------- requirements 说明
class TestRequirementsDocumentsOptionalDep:
    def test_fastmcp_is_documented_as_optional(self):
        from pathlib import Path

        req = Path(__file__).resolve().parents[1] / "requirements.txt"
        text = req.read_text(encoding="utf-8")
        install_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        # fastmcp 只应出现在注释里（可选段），不能进必装列表。
        assert not any("fastmcp" in line for line in install_lines)
        assert "fastmcp" in text


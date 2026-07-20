"""ToolRegistry allowed 白名单参数单测（Phase 2.0 · P0-4）。

验证无状态白名单过滤：
- 未授权工具 execute 返回 INVALID_PARAM error。
- 授权工具正常执行。
- finish 恒被允许（即便不在 allowed 集合里）。
- build_tool_schemas(allowed=...) 只暴露白名单工具（+finish）。
- allowed=None 时行为与一期一致（不过滤）。
- 并行安全：同一 registry 不同 allowed 互不影响。
"""

from wellnessagent import safety_rules  # noqa: F401  (ensure package import path OK)

from WellnessAgent.tools.registry import ToolRegistry, FINISH_TOOL_NAME
from WellnessAgent.tools.response import ToolStatus, ToolErrorCode


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_function("echo", "回显", lambda x: f"echo:{x}")
    reg.register_function("writer", "写操作", lambda x: f"wrote:{x}")
    return reg


class TestExecuteAllowed:
    def test_allowed_tool_runs(self):
        reg = _make_registry()
        resp = reg.execute_tool("echo", "hi", allowed={"echo"})
        assert resp.status == ToolStatus.SUCCESS
        assert resp.text == "echo:hi"

    def test_unauthorized_tool_rejected(self):
        reg = _make_registry()
        resp = reg.execute_tool("writer", "x", allowed={"echo"})
        assert resp.status == ToolStatus.ERROR
        assert resp.error_info["code"] == ToolErrorCode.INVALID_PARAM

    def test_finish_always_allowed(self):
        reg = _make_registry()
        # finish 未在 allowed 中，但白名单归一化应自动补上；未注册 finish 函数，
        # 因此不会被"未授权"拦截，而是走 NOT_FOUND（说明没被白名单拦下）。
        resp = reg.execute_tool(FINISH_TOOL_NAME, "", allowed={"echo"})
        assert resp.status == ToolStatus.ERROR
        assert resp.error_info["code"] == ToolErrorCode.NOT_FOUND

    def test_none_allowed_is_legacy_behavior(self):
        reg = _make_registry()
        resp = reg.execute_tool("writer", "x")  # allowed 默认 None
        assert resp.status == ToolStatus.SUCCESS
        assert resp.text == "wrote:x"

    def test_stateless_parallel_safety(self):
        """同一 registry 用不同 allowed 调用互不影响（无实例状态）。"""
        reg = _make_registry()
        r1 = reg.execute_tool("echo", "a", allowed={"echo"})
        r2 = reg.execute_tool("writer", "b", allowed={"writer"})
        r3 = reg.execute_tool("writer", "c", allowed={"echo"})  # 未授权
        assert r1.status == ToolStatus.SUCCESS
        assert r2.status == ToolStatus.SUCCESS
        assert r3.status == ToolStatus.ERROR


class TestBuildSchemasAllowed:
    def test_filter_exposes_only_allowed_plus_finish(self):
        reg = _make_registry()
        schemas = reg.build_tool_schemas(allowed={"echo"})
        names = {s["function"]["name"] for s in schemas}
        assert names == {"echo", FINISH_TOOL_NAME}

    def test_none_exposes_all(self):
        reg = _make_registry()
        schemas = reg.build_tool_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert {"echo", "writer", FINISH_TOOL_NAME} <= names

    def test_include_finish_false_with_allowed(self):
        reg = _make_registry()
        schemas = reg.build_tool_schemas(include_finish=False, allowed={"echo"})
        names = {s["function"]["name"] for s in schemas}
        assert names == {"echo"}


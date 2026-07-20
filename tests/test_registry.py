"""tools/registry.py 单元测试。

覆盖：
- register_function / register_tool / unregister / 查询
- execute_tool：SUCCESS / NOT_FOUND / EXECUTION_ERROR / INVALID_PARAM / CIRCUIT_OPEN
- 结构化多参数分发 vs 单参数字符串
- build_tool_schemas：函数默认单参 input、显式 parameters、内置 finish、Tool 对象
- execute_tool_text
- 熔断器接入（真实异常计 ERROR，软失败不计入）
"""

import pytest

from WellnessAgent.tools.registry import ToolRegistry, FINISH_TOOL_NAME
from WellnessAgent.tools.response import ToolStatus, ToolErrorCode
from WellnessAgent.tools.circuit_breaker import CircuitBreaker
from WellnessAgent.tools.base import Tool, ToolParameter


class SampleTool(Tool):
    """一个最小 Tool 对象，用于测试 schema 与执行。"""

    def __init__(self):
        super().__init__(name="sample", description="示例工具")

    def run(self, parameters):
        return f"ran:{parameters.get('input', '')}"

    def get_parameters(self):
        return [ToolParameter(name="input", type="string", description="输入", required=True)]


# ==================================================================
# 注册与查询
# ==================================================================
class TestRegistration:
    def test_register_and_query_function(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: x)
        assert reg.has_tool("echo")
        assert reg.get_function("echo") is not None
        assert "echo" in reg.list_tools()

    def test_register_tool_object(self):
        reg = ToolRegistry()
        reg.register_tool(SampleTool())
        assert reg.has_tool("sample")
        assert reg.get_tool("sample") is not None

    def test_unregister_function(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: x)
        reg.unregister("echo")
        assert not reg.has_tool("echo")

    def test_clear(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: x)
        reg.register_tool(SampleTool())
        reg.clear()
        assert reg.list_tools() == []


# ==================================================================
# execute_tool
# ==================================================================
class TestExecuteTool:
    def test_success_single_input(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: f"echo:{x}")
        resp = reg.execute_tool("echo", "hi")
        assert resp.status == ToolStatus.SUCCESS
        assert resp.text == "echo:hi"

    def test_not_found(self):
        reg = ToolRegistry()
        resp = reg.execute_tool("missing", "x")
        assert resp.status == ToolStatus.ERROR
        assert resp.error_info["code"] == ToolErrorCode.NOT_FOUND

    def test_execution_error(self):
        reg = ToolRegistry()

        def boom(_x):
            raise RuntimeError("crash")

        reg.register_function("boom", "崩溃", boom)
        resp = reg.execute_tool("boom", "x")
        assert resp.status == ToolStatus.ERROR
        assert resp.error_info["code"] == ToolErrorCode.EXECUTION_ERROR

    def test_invalid_param(self):
        """结构化 dict 参数与函数签名不匹配 → INVALID_PARAM。"""
        reg = ToolRegistry()
        reg.register_function(
            "adder", "加法", lambda a, b: str(int(a) + int(b)),
            parameters=[
                {"name": "a", "type": "string", "required": True},
                {"name": "b", "type": "string", "required": True},
            ],
        )
        # 传入多余的关键字，触发 TypeError
        resp = reg.execute_tool("adder", {"a": "1", "b": "2", "c": "3"})
        assert resp.status == ToolStatus.ERROR
        assert resp.error_info["code"] == ToolErrorCode.INVALID_PARAM

    def test_structured_dict_dispatch(self):
        reg = ToolRegistry()
        reg.register_function(
            "adder", "加法", lambda a, b: str(int(a) + int(b)),
            parameters=[
                {"name": "a", "type": "string", "required": True},
                {"name": "b", "type": "string", "required": True},
            ],
        )
        resp = reg.execute_tool("adder", {"a": "2", "b": "5"})
        assert resp.text == "7"

    def test_dict_without_schema_degrades_to_input(self):
        """无 schema 的函数收到 dict → 取 input 字段。"""
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: f"echo:{x}")
        resp = reg.execute_tool("echo", {"input": "hello"})
        assert resp.text == "echo:hello"

    def test_tool_object_execution(self):
        reg = ToolRegistry()
        reg.register_tool(SampleTool())
        resp = reg.execute_tool("sample", "abc")
        assert resp.status == ToolStatus.SUCCESS
        assert resp.text == "ran:abc"

    def test_execute_tool_text(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: f"echo:{x}")
        assert reg.execute_tool_text("echo", "hi") == "echo:hi"

    def test_soft_failure_still_success(self):
        """业务软失败（⚠️ 前缀）仍算 SUCCESS，不误判 ERROR。"""
        reg = ToolRegistry()
        reg.register_function("warn", "警告", lambda x: "⚠️ 未找到结果")
        resp = reg.execute_tool("warn", "x")
        assert resp.status == ToolStatus.SUCCESS


# ==================================================================
# 熔断器接入
# ==================================================================
class TestCircuitBreakerIntegration:
    def test_circuit_open_after_failures(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=9999, enabled=True)
        reg = ToolRegistry(circuit_breaker=cb)

        def boom(_x):
            raise RuntimeError("crash")

        reg.register_function("boom", "崩溃", boom)
        reg.execute_tool("boom", "x")
        reg.execute_tool("boom", "x")
        # 达到阈值后，第三次调用应直接短路返回 CIRCUIT_OPEN
        resp = reg.execute_tool("boom", "x")
        assert resp.error_info["code"] == ToolErrorCode.CIRCUIT_OPEN

    def test_soft_failure_does_not_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, enabled=True)
        reg = ToolRegistry(circuit_breaker=cb)
        reg.register_function("warn", "警告", lambda x: "错误：无结果")
        reg.execute_tool("warn", "x")
        reg.execute_tool("warn", "x")
        reg.execute_tool("warn", "x")
        # 软失败不计入 ERROR，熔断器保持关闭
        assert cb.is_open("warn") is False

    def test_success_after_failures_resets(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        reg = ToolRegistry(circuit_breaker=cb)
        calls = {"n": 0}

        def flaky(_x):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("fail")
            return "ok"

        reg.register_function("flaky", "不稳定", flaky)
        reg.execute_tool("flaky", "x")  # fail 1
        reg.execute_tool("flaky", "x")  # fail 2
        reg.execute_tool("flaky", "x")  # success -> reset
        assert cb.get_status("flaky")["failure_count"] == 0


# ==================================================================
# build_tool_schemas
# ==================================================================
class TestBuildToolSchemas:
    def test_default_single_input_schema(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: x)
        schemas = reg.build_tool_schemas(include_finish=False)
        echo_schema = next(s for s in schemas if s["function"]["name"] == "echo")
        props = echo_schema["function"]["parameters"]["properties"]
        assert "input" in props
        assert props["input"]["type"] == "string"

    def test_explicit_parameters_schema(self):
        reg = ToolRegistry()
        reg.register_function(
            "profile_set", "设置画像", lambda **kw: "ok",
            parameters=[
                {"name": "allergies", "type": "array", "items": "string", "description": "过敏原", "required": False},
                {"name": "goal", "type": "string", "description": "目标", "required": True},
            ],
        )
        schemas = reg.build_tool_schemas(include_finish=False)
        schema = next(s for s in schemas if s["function"]["name"] == "profile_set")
        props = schema["function"]["parameters"]["properties"]
        assert props["allergies"]["type"] == "array"
        assert props["allergies"]["items"] == {"type": "string"}
        assert props["goal"]["type"] == "string"
        assert schema["function"]["parameters"]["required"] == ["goal"]

    def test_finish_schema_included(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: x)
        schemas = reg.build_tool_schemas(include_finish=True)
        names = [s["function"]["name"] for s in schemas]
        assert FINISH_TOOL_NAME in names
        finish = next(s for s in schemas if s["function"]["name"] == FINISH_TOOL_NAME)
        assert finish["function"]["parameters"]["required"] == ["answer"]

    def test_finish_excluded(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显", lambda x: x)
        schemas = reg.build_tool_schemas(include_finish=False)
        names = [s["function"]["name"] for s in schemas]
        assert FINISH_TOOL_NAME not in names

    def test_tool_object_schema_included(self):
        reg = ToolRegistry()
        reg.register_tool(SampleTool())
        schemas = reg.build_tool_schemas(include_finish=False)
        names = [s["function"]["name"] for s in schemas]
        assert "sample" in names


# ==================================================================
# get_tools_description
# ==================================================================
class TestToolsDescription:
    def test_description_lists_all(self):
        reg = ToolRegistry()
        reg.register_function("echo", "回显工具", lambda x: x)
        reg.register_tool(SampleTool())
        desc = reg.get_tools_description()
        assert "echo" in desc
        assert "sample" in desc

    def test_description_empty(self):
        reg = ToolRegistry()
        assert reg.get_tools_description() == "暂无可用工具"


"""工具注册表（WellnessAgent 自研升级）。

相比旧版：
- ``execute_tool`` 返回 :class:`ToolResponse`（结构化，含熔断）。
- ``register_function`` 支持可选 ``parameters`` 显式 schema；未提供时默认单参数
  ``input:string``。
- 新增 ``build_tool_schemas`` 生成 OpenAI Function Calling schema 列表（含内置
  ``finish`` 工具）。
- 新增 ``execute_tool_text`` 平滑返回 ``str``，供回退路径/benchmark 复用。
"""

from typing import Optional, Any, Callable, Iterable

from .base import Tool
from .response import ToolResponse, ToolErrorCode
from .circuit_breaker import CircuitBreaker


# 内置 finish 工具名（结束判定，见 react_agent）。
FINISH_TOOL_NAME = "finish"


class ToolRegistry:
    """HelloAgents 风格工具注册表（结构化返回 + 熔断 + FC schema）。"""

    def __init__(self, circuit_breaker: Optional[CircuitBreaker] = None):
        self._tools: dict[str, Tool] = {}
        self._functions: dict[str, dict[str, Any]] = {}
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

    # ------------------------------------------------------------------ 注册
    def register_tool(self, tool: Tool, auto_expand: bool = True):
        """注册 Tool 对象（保留自动展开逻辑）。"""
        if auto_expand and hasattr(tool, "expandable") and tool.expandable:
            expanded_tools = tool.get_expanded_tools()
            if expanded_tools:
                for sub_tool in expanded_tools:
                    if sub_tool.name in self._tools:
                        print(f"⚠️ 警告：工具 '{sub_tool.name}' 已存在，将被覆盖。")
                    self._tools[sub_tool.name] = sub_tool
                print(f"✅ 工具 '{tool.name}' 已展开为 {len(expanded_tools)} 个独立工具")
                return

        if tool.name in self._tools:
            print(f"⚠️ 警告：工具 '{tool.name}' 已存在，将被覆盖。")
        self._tools[tool.name] = tool
        print(f"✅ 工具 '{tool.name}' 已注册。")

    def register_function(
        self,
        name: str,
        description: str,
        func: Callable[..., str],
        parameters: Optional[list[dict]] = None,
    ):
        """直接注册函数工具。

        Args:
            name: 工具名称。
            description: 工具描述。
            func: 工具函数。默认接受单个字符串入参并返回字符串；当提供
                ``parameters`` 时，函数应接受关键字参数（见 ReAct 结构化调用）。
            parameters: 可选的 OpenAI schema ``properties`` 片段列表，每项形如
                ``{"name": "allergies", "type": "array", "description": "...",
                "required": False, "items": "string"}``。未提供时默认单参数
                ``input:string``。
        """
        if name in self._functions:
            print(f"⚠️ 警告：工具 '{name}' 已存在，将被覆盖。")
        self._functions[name] = {
            "description": description,
            "func": func,
            "parameters": parameters,
        }
        print(f"✅ 工具 '{name}' 已注册。")

    def unregister(self, name: str):
        """注销工具。"""
        if name in self._tools:
            del self._tools[name]
            print(f"🗑️ 工具 '{name}' 已注销。")
        elif name in self._functions:
            del self._functions[name]
            print(f"🗑️ 工具 '{name}' 已注销。")
        else:
            print(f"⚠️ 工具 '{name}' 不存在。")

    # ------------------------------------------------------------------ 查询
    def get_tool(self, name: str) -> Optional[Tool]:
        """获取 Tool 对象。"""
        return self._tools.get(name)

    def get_function(self, name: str) -> Optional[Callable]:
        """获取工具函数。"""
        func_info = self._functions.get(name)
        return func_info["func"] if func_info else None

    def has_tool(self, name: str) -> bool:
        """是否存在同名工具。"""
        return name in self._tools or name in self._functions

    # ------------------------------------------------------------------ 白名单
    @staticmethod
    def _normalize_allowed(allowed: Optional[Iterable[str]]) -> Optional[set[str]]:
        """把 ``allowed`` 归一化为集合；``finish`` 始终允许（否则子代理无法收尾）。

        - ``None`` → 返回 ``None`` 表示"不过滤"（一期行为）。
        - 传入集合 → 与 ``{finish}`` 求并，保证 finish 永远可调用。
        """
        if allowed is None:
            return None
        normalized = set(allowed)
        normalized.add(FINISH_TOOL_NAME)
        return normalized

    # ------------------------------------------------------------------ 执行
    def execute_tool(
        self,
        name: str,
        tool_input: Any,
        allowed: Optional[Iterable[str]] = None,
    ) -> ToolResponse:
        """执行工具并返回结构化 :class:`ToolResponse`。

        Args:
            name: 工具名。
            tool_input: 对函数工具为字符串（单参数）或 dict（结构化多参数）；
                对 Tool 对象为字符串。
            allowed: 可选的授权工具白名单（P0-4，无状态过滤）。为 ``None`` 时
                不过滤（一期行为）；非空时命中未授权工具直接返回
                ``INVALID_PARAM`` error（``finish`` 恒被允许）。并行安全：过滤
                只依赖本次调用传入的集合，不引入实例状态。
        """
        allowed_set = self._normalize_allowed(allowed)
        if allowed_set is not None and name not in allowed_set:
            return ToolResponse.error(
                ToolErrorCode.INVALID_PARAM,
                f"工具 '{name}' 未授权给当前子代理。",
            )

        # 熔断检查。
        if self.circuit_breaker.is_open(name):
            return ToolResponse.error(
                ToolErrorCode.CIRCUIT_OPEN,
                f"工具 '{name}' 已被熔断，暂时跳过调用。",
            )

        if name not in self._tools and name not in self._functions:
            response = ToolResponse.error(
                ToolErrorCode.NOT_FOUND, f"未找到名为 '{name}' 的工具。"
            )
            self.circuit_breaker.record_result(name, response)
            return response

        try:
            if name in self._tools:
                tool = self._tools[name]
                raw = tool.run({"input": tool_input if isinstance(tool_input, str) else ""})
                response = self._wrap_text_result(raw)
            else:
                func = self._functions[name]["func"]
                has_schema = self._functions[name].get("parameters")
                if has_schema and isinstance(tool_input, dict):
                    raw = func(**tool_input)
                elif isinstance(tool_input, dict):
                    # 无 schema 但拿到 dict：退化到 input 字段或序列化。
                    raw = func(tool_input.get("input", ""))
                else:
                    raw = func(tool_input)
                response = self._wrap_text_result(raw)
        except TypeError as exc:
            response = ToolResponse.error(
                ToolErrorCode.INVALID_PARAM,
                f"执行工具 '{name}' 参数不匹配: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - 统一兜底为 ERROR
            response = ToolResponse.error(
                ToolErrorCode.EXECUTION_ERROR,
                f"执行工具 '{name}' 时发生异常: {exc}",
            )

        self.circuit_breaker.record_result(name, response)
        return response

    def execute_tool_text(
        self,
        name: str,
        tool_input: Any,
        allowed: Optional[Iterable[str]] = None,
    ) -> str:
        """执行工具并返回 ``str``（``= execute_tool().text``）。"""
        return self.execute_tool(name, tool_input, allowed=allowed).text

    @staticmethod
    def _wrap_text_result(raw: Any) -> ToolResponse:
        """把函数/Tool 的原始返回包裹成 ToolResponse。

        业务工具约定用 ``str`` 返回；软失败前缀（⚠️/错误：）仍算 SUCCESS 文本，
        避免误熔断（真正异常已在上层转 ERROR）。
        """
        text = raw if isinstance(raw, str) else str(raw)
        return ToolResponse.success(text=text)

    # ------------------------------------------------------------------ schema
    def build_tool_schemas(
        self,
        include_finish: bool = True,
        allowed: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """生成 OpenAI Function Calling 的 tools schema 列表。

        Args:
            include_finish: 是否附加内置 finish 工具 schema。
            allowed: 可选授权工具白名单（P0-4，无状态过滤）。为 ``None`` 时暴露
                全部工具（一期行为）；非空时只暴露白名单内的工具（``finish`` 恒被
                允许，只受 ``include_finish`` 控制）。
        """
        allowed_set = self._normalize_allowed(allowed)
        schemas: list[dict] = []

        for tool in self._tools.values():
            if not hasattr(tool, "to_openai_schema"):
                continue
            if allowed_set is not None and tool.name not in allowed_set:
                continue
            schemas.append(tool.to_openai_schema())

        for name, info in self._functions.items():
            if allowed_set is not None and name not in allowed_set:
                continue
            schemas.append(self._function_schema(name, info))

        if include_finish:
            schemas.append(self._finish_schema())
        return schemas

    @staticmethod
    def _function_schema(name: str, info: dict) -> dict:
        """为函数工具构造 schema。无显式 parameters 时用单参数 input:string。"""
        parameters = info.get("parameters")
        if not parameters:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": "工具输入，可为空字符串。",
                            }
                        },
                        "required": [],
                    },
                },
            }

        properties: dict[str, dict] = {}
        required: list[str] = []
        for param in parameters:
            param_name = param["name"]
            prop: dict[str, Any] = {
                "type": param.get("type", "string"),
                "description": param.get("description", ""),
            }
            if prop["type"] == "array":
                prop["items"] = {"type": param.get("items", "string")}
            properties[param_name] = prop
            if param.get("required"):
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    @staticmethod
    def _finish_schema() -> dict:
        """内置 finish 工具的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": FINISH_TOOL_NAME,
                "description": "当你已经有足够信息回答用户时，调用本工具输出最终回答并结束。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "description": "面向用户的完整最终回答。",
                        }
                    },
                    "required": ["answer"],
                },
            },
        }

    # ------------------------------------------------------------------ misc
    def get_tools_description(self) -> str:
        """获取所有可用工具的格式化描述（用于回退版 prompt）。"""
        descriptions = []
        for tool in self._tools.values():
            descriptions.append(f"- {tool.name}: {tool.description}")
        for name, info in self._functions.items():
            descriptions.append(f"- {name}: {info['description']}")
        return "\n".join(descriptions) if descriptions else "暂无可用工具"

    def list_tools(self) -> list[str]:
        """列出所有工具名称。"""
        return list(self._tools.keys()) + list(self._functions.keys())

    def get_all_tools(self) -> list[Tool]:
        """获取所有 Tool 对象。"""
        return list(self._tools.values())

    def clear(self):
        """清空所有工具。"""
        self._tools.clear()
        self._functions.clear()
        print("🧹 所有工具已清空。")


# 全局工具注册表。
global_registry = ToolRegistry()

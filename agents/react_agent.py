"""ReAct Agent实现 - 推理与行动结合的智能体"""

from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Optional, List, Tuple
from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config
from ..core.message import Message
from ..tools.registry import ToolRegistry

# 默认ReAct提示词模板
DEFAULT_REACT_PROMPT = """你是一个具备推理和行动能力的AI助手。你可以通过思考分析问题，然后调用合适的工具来获取信息，最终给出准确的答案。

## 可用工具
{tools}

## 工作流程
请严格输出一个 JSON 对象，每次只能执行一个步骤，格式如下：
{{
  "thought": "分析问题，确定需要什么信息，制定研究策略。",
  "action": {{
    "type": "tool" 或 "finish",
    "name": "工具名，仅 type=tool 时需要",
    "input": "工具输入，仅 type=tool 时需要，可为空字符串",
    "answer": "最终回答，仅 type=finish 时需要"
  }}
}}

## 重要提醒
1. 每次回应必须是合法 JSON，不要输出 JSON 之外的任何额外文本
2. `action.type="tool"` 时，必须提供 `name` 和 `input`
3. 只有当你确信有足够信息回答问题时，才使用 `action.type="finish"`
4. 如果工具返回的信息不够，继续使用其他工具或相同工具的不同参数

## 当前任务
**Question:** {question}

## 已注入上下文
{additional_context}

## 执行历史
{history}

现在开始你的推理和行动："""


@dataclass
class StepRecord:
    """单步 ReAct 执行记录。"""

    step_index: int
    thought: Optional[str]
    action_text: Optional[str]
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None
    observation: Optional[str] = None
    raw_response: Optional[str] = None
    action_debug: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReActRunResult:
    """结构化 ReAct 执行结果。"""

    final_answer: str
    steps: list[StepRecord]
    terminated_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "steps": [step.to_dict() for step in self.steps],
            "terminated_reason": self.terminated_reason,
        }

class ReActAgent(Agent):
    """
    ReAct (Reasoning and Acting) Agent
    
    结合推理和行动的智能体，能够：
    1. 分析问题并制定行动计划
    2. 调用外部工具获取信息
    3. 基于观察结果进行推理
    4. 迭代执行直到得出最终答案
    
    这是一个经典的Agent范式，特别适合需要外部信息的任务。
    """
    
    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: Optional[ToolRegistry] = None,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        max_steps: int = 5,
        custom_prompt: Optional[str] = None
    ):
        """
        初始化ReActAgent

        Args:
            name: Agent名称
            llm: LLM实例
            tool_registry: 工具注册表（可选，如果不提供则创建空的工具注册表）
            system_prompt: 系统提示词
            config: 配置对象
            max_steps: 最大执行步数
            custom_prompt: 自定义提示词模板
        """
        super().__init__(name, llm, system_prompt, config)

        # 如果没有提供tool_registry，创建一个空的
        if tool_registry is None:
            self.tool_registry = ToolRegistry()
        else:
            self.tool_registry = tool_registry

        self.max_steps = max_steps
        self.current_history: List[str] = []

        # 设置提示词模板：用户自定义优先，否则使用默认模板
        self.prompt_template = custom_prompt if custom_prompt else DEFAULT_REACT_PROMPT

    def add_tool(self, tool):
        """
        添加工具到工具注册表
        支持MCP工具的自动展开

        Args:
            tool: 工具实例(可以是普通Tool或MCPTool)
        """
        # 检查是否是MCP工具
        if hasattr(tool, 'auto_expand') and tool.auto_expand:
            # MCP工具会自动展开为多个工具
            if hasattr(tool, '_available_tools') and tool._available_tools:
                for mcp_tool in tool._available_tools:
                    # 创建包装工具
                    from ..tools.base import Tool
                    wrapped_tool = Tool(
                        name=f"{tool.name}_{mcp_tool['name']}",
                        description=mcp_tool.get('description', ''),
                        func=lambda input_text, t=tool, tn=mcp_tool['name']: t.run({
                            "action": "call_tool",
                            "tool_name": tn,
                            "arguments": {"input": input_text}
                        })
                    )
                    self.tool_registry.register_tool(wrapped_tool)
                print(f"✅ MCP工具 '{tool.name}' 已展开为 {len(tool._available_tools)} 个独立工具")
            else:
                self.tool_registry.register_tool(tool)
        else:
            self.tool_registry.register_tool(tool)

    def run(self, input_text: str, **kwargs) -> str:
        """运行 ReAct Agent 并返回最终答案。"""
        return self.run_with_trace(input_text, **kwargs).final_answer

    def run_with_trace(self, input_text: str, **kwargs) -> ReActRunResult:
        """运行 ReAct Agent 并返回结构化步骤追踪。"""
        self.current_history = []
        current_step = 0
        steps: List[StepRecord] = []
        
        print(f"\n🤖 {self.name} 开始处理问题: {input_text}")
        
        while current_step < self.max_steps:
            current_step += 1
            print(f"\n--- 第 {current_step} 步 ---")
            
            # 构建提示词
            tools_desc = self.tool_registry.get_tools_description()
            history_str = "\n".join(self.current_history)
            additional_context = self.build_additional_context(input_text)
            prompt = self.prompt_template.format(
                tools=tools_desc,
                question=input_text,
                history=history_str,
                additional_context=additional_context,
            )
            
            # 调用LLM
            messages = [{"role": "user", "content": prompt}]
            response_text = self._invoke_react_llm(messages, **kwargs)
            
            if not response_text:
                print("❌ 错误：LLM未能返回有效响应。")
                return self._finalize_run(
                    input_text=input_text,
                    final_answer="抱歉，模型未能返回有效响应。",
                    steps=steps,
                    terminated_reason="llm_empty_response",
                )
            
            # 解析输出
            thought, action = self._parse_output(response_text)
            step_record = StepRecord(
                step_index=current_step,
                thought=thought,
                action_text=action,
                raw_response=response_text,
                action_debug=(
                    f"raw_action_source={self._extract_action_source(response_text)} | "
                    f"extracted_action={action or '(none)'}"
                ),
            )
            
            if thought:
                print(f"🤔 思考: {thought}")
            
            if not action:
                print("⚠️ 警告：未能解析出有效的Action，流程终止。")
                steps.append(step_record)
                return self._finalize_run(
                    input_text=input_text,
                    final_answer="抱歉，我未能解析出有效动作，无法继续执行。",
                    steps=steps,
                    terminated_reason="invalid_action",
                )
            
            # 检查是否完成
            if action.startswith("Finish"):
                final_answer = self._parse_action_input(action)
                print(f"🎉 最终答案: {final_answer}")
                steps.append(step_record)
                return self._finalize_run(
                    input_text=input_text,
                    final_answer=final_answer,
                    steps=steps,
                    terminated_reason="finished",
                )
            
            # 执行工具调用
            tool_name, tool_input = self._parse_action(action)
            step_record.tool_name = tool_name
            step_record.tool_input = tool_input
            if not tool_name or tool_input is None:
                self.current_history.append("Observation: 无效的Action格式，请检查。")
                step_record.observation = "无效的Action格式，请检查。"
                steps.append(step_record)
                continue
            
            print(f"🎬 行动: {tool_name}[{tool_input}]")
            
            # 调用工具
            observation = self.tool_registry.execute_tool(tool_name, tool_input)
            print(f"👀 观察: {observation}")
            step_record.observation = observation
            steps.append(step_record)
            
            # 更新历史
            if thought:
                self.current_history.append(f"Thought: {thought}")
            self.current_history.append(f"Action: {action}")
            self.current_history.append(f"Observation: {observation}")
        
        print("⏰ 已达到最大步数，流程终止。")
        return self._finalize_run(
            input_text=input_text,
            final_answer="抱歉，我无法在限定步数内完成这个任务。",
            steps=steps,
            terminated_reason="max_steps",
        )
    
    def _parse_output(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """优先按 JSON 协议解析，失败时回退旧格式解析。"""
        json_parsed = self._parse_json_output(text)
        if json_parsed is not None:
            return json_parsed

        thought_match = re.search(r"Thought:\s*(.*?)(?:\nAction:|\Z)", text, re.DOTALL)
        action_match = re.search(r"Action:\s*(.*)", text, re.DOTALL)
        
        thought = thought_match.group(1).strip() if thought_match else None
        if action_match:
            action = self._extract_first_action(action_match.group(1))
        else:
            action = self._extract_first_action(text)
        
        return thought, action

    def _invoke_react_llm(self, messages: list[dict[str, str]], **kwargs) -> str:
        """Invoke the model with JSON preference and graceful fallback."""
        llm_kwargs = dict(kwargs)
        llm_kwargs.setdefault("temperature", 0.1)
        try:
            llm_kwargs.setdefault("response_format", {"type": "json_object"})
            return self.llm.invoke(messages, **llm_kwargs)
        except Exception:
            llm_kwargs.pop("response_format", None)
            return self.llm.invoke(messages, **llm_kwargs)

    def _parse_json_output(self, text: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
        """Parse structured JSON output from the model."""
        payload = self._extract_json_object(text)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None
        thought = str(data.get("thought", "")).strip() or None
        action_data = data.get("action")
        if not isinstance(action_data, dict):
            return thought, None

        action_type = str(action_data.get("type", "")).strip().lower()
        if action_type == "tool":
            tool_name = str(action_data.get("name", "")).strip()
            tool_input = action_data.get("input", "")
            if not tool_name:
                return thought, None
            if tool_input is None:
                tool_input = ""
            return thought, f"{tool_name}[{tool_input}]"
        if action_type == "finish":
            answer = action_data.get("answer", "")
            if answer is None:
                answer = ""
            return thought, f"Finish[{answer}]"
        return thought, None

    def _extract_json_object(self, text: str) -> Optional[str]:
        """Extract the first balanced JSON object from text."""
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()

        start = cleaned.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False
        collected: list[str] = []
        for char in cleaned[start:]:
            collected.append(char)
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return "".join(collected).strip()
        return None

    def _extract_action_source(self, text: str) -> str:
        """Return the raw text region from which action extraction was attempted."""
        action_match = re.search(r"Action:\s*(.*)", text, re.DOTALL)
        if action_match:
            return action_match.group(1).strip()
        return text.strip()
    
    def _parse_action(self, action_text: str) -> Tuple[Optional[str], Optional[str]]:
        """解析行动文本，提取工具名称和输入"""
        match = re.match(r"(\w+)\[(.*)\]\s*\Z", action_text, re.DOTALL)
        if match:
            return match.group(1), match.group(2)
        return None, None
    
    def _parse_action_input(self, action_text: str) -> str:
        """解析行动输入"""
        match = re.match(r"\w+\[(.*)\]\s*\Z", action_text, re.DOTALL)
        return match.group(1) if match else ""

    def _extract_first_action(self, action_block: str) -> Optional[str]:
        """从 Action 段中提取第一个合法动作，忽略后续多余文本。"""
        if not action_block:
            return None

        normalized = action_block.strip()
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]

        for line in lines:
            candidate = line
            if candidate.startswith("- "):
                candidate = candidate[2:].strip()
            candidate = candidate.strip("`").strip()
            extracted = self._extract_action_from_text(candidate)
            if extracted:
                return extracted

        # 回退：兼容模型把 Finish[...] 或 tool[...] 直接写在多行块开头
        stripped = normalized.strip("`").strip()
        return self._extract_action_from_text(stripped)

    def _extract_action_from_text(self, text: str) -> Optional[str]:
        """Extract the first balanced action starting at the beginning of text."""
        if not text:
            return None
        match = re.match(r"^([A-Za-z0-9_]+)\[", text)
        if not match:
            return None
        return self._extract_bracketed_action(text, match.group(1))

    def _extract_bracketed_action(self, text: str, action_name: str) -> Optional[str]:
        """Extract one balanced `name[...]` action without regex backtracking."""
        prefix = f"{action_name}["
        if not text.startswith(prefix):
            return None

        depth = 0
        collected: list[str] = []
        for char in text:
            collected.append(char)
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return "".join(collected).strip().rstrip("`").strip()

        return None

    def build_additional_context(self, input_text: str) -> str:
        """Build optional prompt context injected before each reasoning step."""
        return "暂无附加上下文。"

    def _finalize_run(
        self,
        input_text: str,
        final_answer: str,
        steps: List[StepRecord],
        terminated_reason: str,
    ) -> ReActRunResult:
        """保存历史并返回结构化执行结果。"""
        self.add_message(Message(input_text, "user"))
        self.add_message(Message(final_answer, "assistant"))
        return ReActRunResult(
            final_answer=final_answer,
            steps=steps,
            terminated_reason=terminated_reason,
        )

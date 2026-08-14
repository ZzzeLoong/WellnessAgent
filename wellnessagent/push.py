"""主动推送：计划配置 · 内容生成 · inbox 投递（三期 R10，方案 §4.2–4.4）。

三块职责，刻意分开以便单测：

===================  ==========================================================
:class:`PushPlanStore`   ``logs/push/<user_id>.plan.json`` —— 开关 / 时刻 / 类型
:class:`PushInbox`       ``logs/push/<user_id>.inbox.jsonl`` —— 落盘收件箱 + 已读
:class:`PushGenerator`   读画像与建议 → 一次轻量 LLM → :class:`PushMessage`
===================  ==========================================================

**R10 最关键的设计点是会话隔离**（方案 §4.3）：推送内容由 agent 生成，但绝不能
污染用户的对话 messages —— 否则用户下次打开对话，历史里凭空多出几轮「自言自语」。

隔离口径：

======================  ====================================================
对话 session            跑在独立 session ``push-<YYYYMMDD>``，跑完切回
长期记忆（读）           **正常读**，这正是「个性化而非模板」的来源
长期记忆（写）           推送轮不触发 distill（``_push_mode``）
working memory          推送 session 独立，不碰用户当前会话的临时上下文
======================  ====================================================

内容生成走**轻量单次 LLM** 而不是 composite pipeline：推送轮没有用户在场，
composite 的 HITL 关卡一旦挂起就永远没人确认，会把这条推送永久卡死。
"""

from __future__ import annotations

import json
import os
import random
import string
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .prompts import PUSH_GENERATION_PROMPT, push_generation_task


DEFAULT_PUSH_DIR = "logs/push"
DEFAULT_PUSH_TIME = "08:30"

#: 推送类型。
KIND_DAILY = "daily_suggestion"
KIND_FOLLOWUP = "followup"
VALID_KINDS: frozenset[str] = frozenset({KIND_DAILY, KIND_FOLLOWUP})

#: inbox 正文上限（推送是通知，不是文章）。
BODY_MAX = 2000
TITLE_MAX = 60


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def push_enabled() -> bool:
    """R10 调度总开关（``WELLNESS_PUSH_ENABLED``，默认关闭）。

    关闭时 tick 线程不启动 —— CI 与默认部署完全不受影响（方案 §4.1）。
    注意：``/api/push/run-now`` **不受此开关限制**，否则 demo 无法演示。
    """
    return _env_flag("WELLNESS_PUSH_ENABLED", False)


def new_push_id() -> str:
    """``p-YYYYMMDD-HHMMSS-xxxx``（与 advice / session id 同款风格）。"""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"p-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{suffix}"


def _safe_user(user_id: str) -> str:
    safe = str(user_id or "unknown").replace("/", "_").replace("\\", "_")
    return "unknown" if safe in {".", ".."} else safe


def _normalize_time(value: str, fallback: str = DEFAULT_PUSH_TIME) -> str:
    """把 ``"8:30"`` / ``"08:30"`` 归一化为 ``"08:30"``；非法值回落 ``fallback``。"""
    text = (value or "").strip()
    if not text:
        return fallback
    parts = text.split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


# --------------------------------------------------------------------------- #
# 推送计划
# --------------------------------------------------------------------------- #
@dataclass
class PushPlan:
    """一个用户的推送计划（方案 §4.2）。"""

    user_id: str
    enabled: bool = False
    daily_at: str = DEFAULT_PUSH_TIME
    timezone: str = "Asia/Shanghai"
    kinds: list[str] = field(default_factory=lambda: [KIND_DAILY, KIND_FOLLOWUP])
    #: 最近一次成功推送的日期（``YYYY-MM-DD``），用于「每天至多一条」。
    last_run_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, user_id: str, data: dict[str, Any] | None) -> "PushPlan":
        data = data or {}
        kinds = [k for k in (data.get("kinds") or []) if k in VALID_KINDS]
        return cls(
            user_id=user_id,
            enabled=bool(data.get("enabled", False)),
            daily_at=_normalize_time(str(data.get("daily_at") or "")),
            timezone=str(data.get("timezone") or "Asia/Shanghai"),
            kinds=kinds or [KIND_DAILY, KIND_FOLLOWUP],
            last_run_date=str(data.get("last_run_date") or ""),
        )

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """是否到点该推送了。

        判定：已启用 + 今天还没推过 + 当前时刻已过 ``daily_at``。
        「今天还没推过」用 ``last_run_date`` 而不是精确时刻比对 —— tick 每 60s
        跑一次，用日期去重才不会在 08:30–08:31 之间连推两条。
        """
        if not self.enabled:
            return False
        moment = now or datetime.now()
        if self.last_run_date == moment.strftime("%Y-%m-%d"):
            return False
        target = _normalize_time(self.daily_at)
        return moment.strftime("%H:%M") >= target


class PushPlanStore:
    """推送计划的读写（``logs/push/<user_id>.plan.json``）。"""

    def __init__(self, push_dir: Optional[str] = None):
        self.push_dir = Path(push_dir or os.getenv("WELLNESS_PUSH_DIR", DEFAULT_PUSH_DIR))
        self._lock = threading.RLock()

    def path_for(self, user_id: str) -> Path:
        return self.push_dir / f"{_safe_user(user_id)}.plan.json"

    def load(self, user_id: str) -> PushPlan:
        """读计划；不存在时返回**默认关闭**的计划（不落盘）。"""
        path = self.path_for(user_id)
        if not path.exists():
            return PushPlan(
                user_id=user_id,
                daily_at=_normalize_time(
                    os.getenv("WELLNESS_PUSH_DEFAULT_TIME", DEFAULT_PUSH_TIME)
                ),
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件按默认计划处理
            return PushPlan(user_id=user_id)
        return PushPlan.from_dict(user_id, data)

    def save(self, plan: PushPlan) -> PushPlan:
        """原子落盘计划。"""
        path = self.path_for(plan.user_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        return plan

    def update(self, user_id: str, **changes: Any) -> PushPlan:
        """局部更新计划字段并落盘（``/api/push/plan`` 用）。"""
        with self._lock:
            plan = self.load(user_id)
            if "enabled" in changes and changes["enabled"] is not None:
                plan.enabled = bool(changes["enabled"])
            if changes.get("daily_at"):
                plan.daily_at = _normalize_time(str(changes["daily_at"]), plan.daily_at)
            if changes.get("timezone"):
                plan.timezone = str(changes["timezone"])
            if changes.get("kinds") is not None:
                kinds = [k for k in changes["kinds"] if k in VALID_KINDS]
                if kinds:
                    plan.kinds = kinds
            if changes.get("last_run_date") is not None:
                plan.last_run_date = str(changes["last_run_date"])
            return self.save(plan)

    def mark_ran(self, user_id: str, when: Optional[date] = None) -> PushPlan:
        """记录「今天已推送」，避免同日重复。"""
        day = (when or date.today()).strftime("%Y-%m-%d")
        return self.update(user_id, last_run_date=day)

    def list_users(self) -> list[str]:
        """列出所有有计划文件的用户（tick 线程扫描用）。"""
        if not self.push_dir.exists():
            return []
        users: list[str] = []
        for path in sorted(self.push_dir.glob("*.plan.json")):
            name = path.name[: -len(".plan.json")]
            if name:
                users.append(name)
        return users


# --------------------------------------------------------------------------- #
# 收件箱
# --------------------------------------------------------------------------- #
@dataclass
class PushMessage:
    """一条推送消息（inbox 条目，方案 §4.4）。"""

    push_id: str
    user_id: str
    kind: str
    title: str
    body: str
    ts: str = field(default_factory=lambda: datetime.now().isoformat())
    read: bool = False
    ref_advice_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["PushMessage"]:
        data = data or {}
        push_id = str(data.get("push_id") or "").strip()
        if not push_id:
            return None
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        payload = {k: v for k, v in data.items() if k in known}
        payload["push_id"] = push_id
        payload["read"] = bool(payload.get("read", False))
        return cls(**payload)


class PushInbox:
    """落盘收件箱（``logs/push/<user_id>.inbox.jsonl``）。

    与 :class:`AdviceStore` 同款 append-only 语义：标记已读 = 追加一条同
    ``push_id`` 的新行，读时取最后一条。
    """

    def __init__(self, push_dir: Optional[str] = None):
        self.push_dir = Path(push_dir or os.getenv("WELLNESS_PUSH_DIR", DEFAULT_PUSH_DIR))
        self._lock = threading.RLock()

    def path_for(self, user_id: str) -> Path:
        return self.push_dir / f"{_safe_user(user_id)}.inbox.jsonl"

    def _append_line(self, user_id: str, payload: dict) -> None:
        path = self.path_for(user_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append(self, message: PushMessage) -> str:
        """投递一条推送到 inbox，返回 ``push_id``。"""
        if not message.push_id:
            message.push_id = new_push_id()
        message.title = (message.title or "")[:TITLE_MAX]
        message.body = (message.body or "")[:BODY_MAX]
        self._append_line(message.user_id, message.to_dict())
        return message.push_id

    def _read_all(self, user_id: str) -> list[PushMessage]:
        """读全量并按 ``push_id`` 折叠为最后一条（保持投递顺序）。"""
        path = self.path_for(user_id)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        order: list[str] = []
        latest: dict[str, PushMessage] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            message = PushMessage.from_dict(data)
            if message is None:
                continue
            if message.push_id not in order:
                order.append(message.push_id)
            latest[message.push_id] = message
        return [latest[pid] for pid in order]

    def list(
        self, user_id: str, unread_only: bool = False, limit: int = 50
    ) -> list[PushMessage]:
        """列出收件箱，**最新的在前**。"""
        messages = self._read_all(user_id)
        if unread_only:
            messages = [m for m in messages if not m.read]
        messages.sort(key=lambda m: (m.ts, m.push_id), reverse=True)
        return messages[:limit] if limit and limit > 0 else messages

    def unread_count(self, user_id: str) -> int:
        """未读数（前端角标）。"""
        return sum(1 for m in self._read_all(user_id) if not m.read)

    def mark_read(self, user_id: str, push_id: str) -> bool:
        """标记一条已读；不存在返回 False。"""
        with self._lock:
            for message in self._read_all(user_id):
                if message.push_id == push_id:
                    if message.read:
                        return True
                    message.read = True
                    self._append_line(user_id, message.to_dict())
                    return True
            return False

    def mark_all_read(self, user_id: str) -> int:
        """全部标记已读，返回受影响条数。"""
        with self._lock:
            count = 0
            for message in self._read_all(user_id):
                if not message.read:
                    message.read = True
                    self._append_line(user_id, message.to_dict())
                    count += 1
            return count


# --------------------------------------------------------------------------- #
# 内容生成
# --------------------------------------------------------------------------- #
class PushGenerator:
    """把用户画像 / 长期记忆 / 待回访建议合成一条推送（方案 §4.3）。

    依赖注入 agent 而非自造：画像读取、记忆提纯、advice_store 全都挂在 agent 上。
    但**只读**它们，并通过 ``_push_mode`` + 独立 session 保证写路径隔离。
    """

    def __init__(self, agent: Any, inbox: Optional[PushInbox] = None):
        self.agent = agent
        self.inbox = inbox or PushInbox()

    # ------------------------------------------------------------------ public
    def generate(
        self,
        kinds: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[PushMessage]:
        """生成一条推送消息（**不投递**）；无可推送内容时返回 None。

        「无可推送内容」的典型场景：只订阅了 followup 但没有到期建议 —— 此时
        不该硬造一条通用鸡汤发给用户。
        """
        moment = now or datetime.now()
        # 只有 ``None``（未指定）才回落到默认全订阅；显式传空列表意味着
        # 「什么都不推」，不能被 ``or`` 悄悄变成全订阅。
        requested = [KIND_DAILY, KIND_FOLLOWUP] if kinds is None else list(kinds)
        wanted = {k for k in requested if k in VALID_KINDS}
        if not wanted:
            return None

        pending = self._collect_pending_advice() if KIND_FOLLOWUP in wanted else []
        if KIND_DAILY not in wanted and not pending:
            # 只要回访、但没有到期建议 → 不打扰。
            return None

        kind = self._resolve_kind(wanted, bool(pending))
        profile_summary = self._safe_call(
            self.agent.service.get_current_profile_summary, default=""
        )
        memory_summary = self._safe_call(
            lambda: self.agent.service.get_distilled_memory_summary(limit=4), default=""
        )

        task = push_generation_task(
            kind=kind,
            profile_summary=profile_summary,
            memory_summary=memory_summary,
            pending_advice=pending,
            date_text=moment.strftime("%Y-%m-%d"),
        )
        raw = self._invoke_llm(task)
        if not raw:
            return None

        title, body = self._split_title_body(raw, kind)
        if not body:
            return None
        return PushMessage(
            push_id=new_push_id(),
            user_id=self.agent.user_id,
            kind=kind,
            title=title,
            body=body,
            ts=moment.isoformat(),
            ref_advice_id=pending[0]["advice_id"] if pending else "",
        )

    def deliver(self, message: PushMessage) -> str:
        """投递到 inbox（主通道）+ webhook（预留），返回 ``push_id``。

        webhook 失败只记 trace，**不影响 inbox** —— 外部通道不可靠不该让用户
        丢掉这条推送（方案 §4.4）。
        """
        push_id = self.inbox.append(message)
        self._log("push_delivered", {"push_id": push_id, "channel": "inbox"})
        self._maybe_webhook(message)
        # 回访推送：标记该建议「已发起回访」，避免明天再问一遍。
        if message.ref_advice_id:
            try:
                self.agent.advice_store.mark_followed_up(
                    self.agent.user_id, message.ref_advice_id
                )
            except Exception:  # noqa: BLE001
                pass
        return push_id

    def run_once(
        self,
        kinds: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[PushMessage]:
        """生成 + 投递一条推送（``/api/push/run-now`` 与 tick 共用入口）。

        全程在**推送模式**下执行：独立 session + 不触发 distill（方案 §4.3）。
        """
        with _push_session(self.agent, now=now):
            message = self.generate(kinds=kinds, now=now)
            if message is None:
                return None
            self.deliver(message)
            return message

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _resolve_kind(wanted: set[str], has_pending: bool) -> str:
        """两类可合并为一条推送（方案 §4.3）。"""
        if KIND_DAILY in wanted and has_pending:
            return f"{KIND_DAILY}+{KIND_FOLLOWUP}"
        if has_pending:
            return KIND_FOLLOWUP
        return KIND_DAILY

    def _collect_pending_advice(self, limit: int = 3) -> list[dict[str, Any]]:
        """取到期待回访的建议（供 prompt 与 ``ref_advice_id``）。"""
        try:
            due = self.agent.advice_store.due_for_followup(
                self.agent.user_id, limit=limit
            )
        except Exception:  # noqa: BLE001
            return []
        items: list[dict[str, Any]] = []
        for record in due:
            age_text = ""
            try:
                from .agent import _humanize_age

                age_text = _humanize_age(record.created_at)
            except Exception:  # noqa: BLE001
                age_text = ""
            items.append(
                {
                    "advice_id": record.advice_id,
                    "summary": record.summary_preview(),
                    "age_text": age_text,
                }
            )
        return items

    def _invoke_llm(self, task: str) -> str:
        """一次轻量 LLM 调用（方案 §4.3：不走 composite，避免 HITL 挂起死锁）。"""
        llm = getattr(self.agent, "llm", None)
        if llm is None:
            self._log("push_error", {"message": "llm unavailable"})
            return ""
        messages = [
            {"role": "system", "content": PUSH_GENERATION_PROMPT},
            {"role": "user", "content": task},
        ]
        started = datetime.now()
        try:
            raw = llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            self._log("push_error", {"message": str(exc)[:200]})
            return ""
        latency_ms = int((datetime.now() - started).total_seconds() * 1000)
        self._log("push_generated", {"latency_ms": latency_ms})
        return str(raw or "").strip()

    @staticmethod
    def _split_title_body(raw: str, kind: str) -> tuple[str, str]:
        """按「首行标题 + 空行 + 正文」切分模型输出。

        模型不守格式时（只给一段话）回落为「用 kind 的默认标题 + 全文正文」——
        推送没有标题也能发，但不能因为格式不对就丢掉内容。
        """
        text = (raw or "").strip()
        if not text:
            return "", ""
        default_title = {
            KIND_FOLLOWUP: "关于上次的建议",
            KIND_DAILY: "今日饮食建议",
        }.get(kind, "今日饮食建议")

        lines = text.splitlines()
        first = lines[0].strip().lstrip("#").strip() if lines else ""
        rest = "\n".join(lines[1:]).strip()
        # 首行像标题（够短且后面还有正文）才当标题用。
        if first and rest and len(first) <= TITLE_MAX:
            return first, rest
        return default_title, text

    def _maybe_webhook(self, message: PushMessage) -> None:
        """外部投递预留：``WELLNESS_PUSH_WEBHOOK_URL`` 非空时 POST 一份 JSON。

        用标准库 ``urllib`` 而不是 requests：这条路径是可选增强，不值得为它
        在核心链路上引入第三方依赖。失败只记 trace。
        """
        url = (os.getenv("WELLNESS_PUSH_WEBHOOK_URL") or "").strip()
        if not url:
            return
        try:
            import urllib.request

            payload = json.dumps(message.to_dict(), ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            self._log("push_delivered", {"push_id": message.push_id, "channel": "webhook"})
        except Exception as exc:  # noqa: BLE001
            self._log("push_error", {"message": f"webhook failed: {str(exc)[:160]}"})

    @staticmethod
    def _safe_call(func, default=""):
        try:
            return func()
        except Exception:  # noqa: BLE001
            return default

    def _log(self, event: str, payload: dict) -> None:
        logger = getattr(self.agent, "trace_logger", None)
        if logger is None:
            return
        try:
            logger.log_event(event, payload)
        except Exception:  # noqa: BLE001
            pass


class _push_session:
    """推送轮的会话隔离上下文（方案 §4.3）。

    进入时切到独立 session ``push-<YYYYMMDD>`` 并打开 ``_push_mode``；
    退出时**无条件**恢复原 session 与标记 —— 即便生成过程抛异常，用户的
    对话上下文也不会被留在推送 session 上。
    """

    def __init__(self, agent: Any, now: Optional[datetime] = None):
        self.agent = agent
        self.now = now or datetime.now()
        self._prev_session: Optional[str] = None
        self._prev_mode: bool = False
        self._prev_messages: list = []

    def __enter__(self):
        agent = self.agent
        self._prev_session = getattr(agent, "session_id", None)
        self._prev_mode = getattr(agent, "_push_mode", False)
        self._prev_messages = list(getattr(agent, "messages", []) or [])
        agent._push_mode = True
        push_session_id = f"push-{self.now.strftime('%Y%m%d')}"
        try:
            agent.select_session(push_session_id)
        except Exception:  # noqa: BLE001 - 切不过去也要继续，至少 _push_mode 生效
            pass
        return agent

    def __exit__(self, exc_type, exc, tb) -> None:
        agent = self.agent
        agent._push_mode = self._prev_mode
        try:
            if self._prev_session:
                agent.select_session(self._prev_session)
            else:
                # 原本没有活跃 session：恢复为「未开启」而不是留在推送 session。
                agent.session_id = None
            agent.messages = self._prev_messages
            if getattr(agent, "history_manager", None) is not None:
                agent.history_manager.set_history(agent.messages)
        except Exception:  # noqa: BLE001
            pass


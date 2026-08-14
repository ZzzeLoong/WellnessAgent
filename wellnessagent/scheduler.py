"""推送调度：自研轻量 tick 线程（三期 R10，方案 §4.1）。

**为什么自研而不用 APScheduler**：零新依赖，且**时钟可注入** —— ``run_due(now=...)``
是纯函数式入口，单测直接调它，不需要真的等到 08:30，也不需要 sleep。
APScheduler 功能更强，但与现有同步架构收益有限；外部 cron 则让「agent 会主动」
这件事完全不体现在项目里，demo 价值弱。

线程模型::

    start() → daemon Thread
                └─ 每 WELLNESS_PUSH_TICK_SECONDS 秒醒一次
                     └─ run_due(now)  ← 单测直接调这层
                          └─ 逐用户判定 plan.is_due() → PushGenerator.run_once()

**必须配合 P3.0-3**：tick 线程通过 ``deps.get_agent`` 访问 ``_AGENTS``，
无锁会与 HTTP 请求线程竞态（那个锁已在批次 1 加好）。

``WELLNESS_PUSH_ENABLED`` 默认 ``false``，因此默认部署与 CI 不会启动任何线程。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from .push import (
    PushGenerator,
    PushInbox,
    PushMessage,
    PushPlan,
    PushPlanStore,
    push_enabled,
)


DEFAULT_TICK_SECONDS = 60


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass
class PushResult:
    """一次 ``run_due`` 中单个用户的处理结果（供单测与 API 观测）。"""

    user_id: str
    delivered: bool
    push_id: str = ""
    kind: str = ""
    reason: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "delivered": self.delivered,
            "push_id": self.push_id,
            "kind": self.kind,
            "reason": self.reason,
            "title": self.title,
        }


class PushScheduler:
    """按计划扫描并投递推送的轻量调度器。

    Args:
        agent_provider: ``(user_id) -> agent``。生产传 ``deps.get_agent``；
            单测传假 agent 工厂，完全不碰 LRU 与真实依赖。
        clock: ``() -> datetime``，**可注入** —— 这是本类可单测的关键。
        tick_seconds: 扫描间隔；默认读 ``WELLNESS_PUSH_TICK_SECONDS``。
    """

    def __init__(
        self,
        agent_provider: Optional[Callable[[str], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        tick_seconds: Optional[int] = None,
        plan_store: Optional[PushPlanStore] = None,
        inbox: Optional[PushInbox] = None,
    ):
        self._agent_provider = agent_provider
        self.clock = clock or datetime.now
        self.tick_seconds = tick_seconds or _env_int(
            "WELLNESS_PUSH_TICK_SECONDS", DEFAULT_TICK_SECONDS
        )
        self.plan_store = plan_store or PushPlanStore()
        self.inbox = inbox or PushInbox()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        #: 最近一次 tick 的结果（供 ``/api/push/status`` 观测）。
        self.last_results: list[PushResult] = []
        self.last_tick_at: str = ""

    # ------------------------------------------------------------------ agent
    def _get_agent(self, user_id: str):
        """取该用户的 agent 实例。

        默认走 ``deps.get_agent``（带 LRU 与会话重建）；**延迟导入**避免
        ``scheduler`` ← ``deps`` ← ``agent`` 的循环依赖。
        """
        if self._agent_provider is not None:
            return self._agent_provider(user_id)
        from .server.deps import get_agent

        return get_agent(user_id)

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """启动 tick 线程。总开关关闭或已在运行时返回 False（幂等）。"""
        if not push_enabled():
            return False
        with self._lock:
            if self.running:
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="wellness-push-scheduler", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """停止 tick 线程并等待退出。"""
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        """tick 主循环。用 ``Event.wait`` 而不是 ``sleep`` —— stop 能立刻唤醒。"""
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception:  # noqa: BLE001 - 单次 tick 失败不该终结整个线程
                pass
            self._stop_event.wait(self.tick_seconds)

    # ------------------------------------------------------------------ 核心入口
    def run_due(self, now: Optional[datetime] = None) -> list[PushResult]:
        """扫描所有计划，为到期用户生成并投递推送。

        这是**纯函数式入口**：单测注入 ``now`` 直接调用，无需启动线程、无需等待。
        单个用户失败只记入结果，不影响其他用户（失败隔离）。
        """
        moment = now or self.clock()
        results: list[PushResult] = []
        for user_id in self.plan_store.list_users():
            results.append(self._run_for_user(user_id, moment))
        self.last_results = results
        self.last_tick_at = moment.isoformat()
        return results

    def _run_for_user(self, user_id: str, now: datetime) -> PushResult:
        """处理单个用户：判定到期 → 生成投递 → 记录 ``last_run_date``。"""
        plan = self.plan_store.load(user_id)
        if not plan.enabled:
            return PushResult(user_id=user_id, delivered=False, reason="disabled")
        if not plan.is_due(now):
            return PushResult(user_id=user_id, delivered=False, reason="not_due")
        return self.run_now(user_id, plan=plan, now=now, mark=True)

    def run_now(
        self,
        user_id: str,
        plan: Optional[PushPlan] = None,
        now: Optional[datetime] = None,
        mark: bool = True,
    ) -> PushResult:
        """立即为某用户生成并投递一条推送（``/api/push/run-now`` 的实现）。

        **不检查 ``push_enabled()`` 与 ``plan.enabled``** —— demo 必须能在不开
        全局调度的情况下立刻演示一条推送（方案 §4.2）。到期判定由调用方负责。
        """
        moment = now or self.clock()
        plan = plan or self.plan_store.load(user_id)

        try:
            agent = self._get_agent(user_id)
        except Exception as exc:  # noqa: BLE001
            return PushResult(
                user_id=user_id, delivered=False, reason=f"agent unavailable: {exc}"
            )

        self._log(agent, "push_scheduled", {"user_id": user_id, "kinds": list(plan.kinds)})
        generator = PushGenerator(agent, inbox=self.inbox)
        try:
            message: Optional[PushMessage] = generator.run_once(
                kinds=plan.kinds, now=moment
            )
        except Exception as exc:  # noqa: BLE001 - 失败隔离
            self._log(agent, "push_error", {"message": str(exc)[:200]})
            return PushResult(user_id=user_id, delivered=False, reason=f"error: {exc}")

        if message is None:
            return PushResult(user_id=user_id, delivered=False, reason="no_content")
        if mark:
            try:
                self.plan_store.mark_ran(user_id, when=moment.date())
            except Exception:  # noqa: BLE001
                pass
        return PushResult(
            user_id=user_id,
            delivered=True,
            push_id=message.push_id,
            kind=message.kind,
            title=message.title,
        )

    @staticmethod
    def _log(agent: Any, event: str, payload: dict) -> None:
        logger = getattr(agent, "trace_logger", None)
        if logger is None:
            return
        try:
            logger.log_event(event, payload)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 进程级单例（FastAPI lifespan 用）
# --------------------------------------------------------------------------- #
_SCHEDULER: Optional[PushScheduler] = None
_SCHEDULER_LOCK = threading.RLock()


def get_scheduler() -> PushScheduler:
    """返回进程级调度器（懒构造）。"""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = PushScheduler()
        return _SCHEDULER


def reset_scheduler() -> None:
    """丢弃单例并停掉线程（供单测切换 ``WELLNESS_PUSH_DIR``）。"""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is not None:
            _SCHEDULER.stop()
        _SCHEDULER = None


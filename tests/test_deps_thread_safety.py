"""wellnessagent/server/deps.py 线程安全单测（Phase 3.0 · P3.0-3）。

背景：``_AGENTS`` 是一个裸 ``OrderedDict``，``get_agent`` 里的
「``in`` 判定 → 构造实例 → 写入 → ``move_to_end`` → 淘汰」序列在多线程下非原子。
三期 R10 会引入推送 tick 线程并发调用 ``get_agent``，无锁会出现两类问题：

1. **实例重复**：两个线程同时 miss，同一 ``user_id`` 被构造两次，其中一个实例
   （连带其 session/working memory）被默默丢弃；
2. **KeyError**：LRU 淘汰与 ``move_to_end`` 交错。

本测试用「构造函数里 sleep」的假 agent 放大竞态窗口，因此在**未加锁**的实现上
会稳定复现实例重复；加锁后必须全绿。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

import pytest

from WellnessAgent.wellnessagent.server import deps


THREADS = 32


class FakeSessionStore:
    def __init__(self):
        self.loaded: list[str] = []

    def latest_session_id(self, user_id: str):
        return None

    def list_sessions(self, user_id: str):
        return []


class FakeAgent:
    """轻量 agent 替身；构造期 sleep 用于放大竞态窗口。"""

    construct_count = 0
    construct_lock = threading.Lock()
    construct_delay = 0.004

    def __init__(self, user_id: str, **kwargs):
        with FakeAgent.construct_lock:
            FakeAgent.construct_count += 1
            self.serial = FakeAgent.construct_count
        self.user_id = user_id
        self.session_store = FakeSessionStore()
        self.cleaned = 0
        self.session_id = None
        # 模拟真实构造成本（嵌入模型 / 向量库 / RAG 初始化）。
        time.sleep(FakeAgent.construct_delay)

    def load_session(self, session_id: str):
        self.session_id = session_id

    def new_session(self) -> str:
        self.session_id = f"s-{self.user_id}-{self.serial}"
        return self.session_id

    def cleanup(self):
        self.cleaned += 1


@pytest.fixture(autouse=True)
def isolated_agents(monkeypatch):
    """每个测试用干净的缓存与假 agent，测试结束恢复。"""
    monkeypatch.setattr(deps, "WellnessPlanningAgent", FakeAgent)
    monkeypatch.setattr(deps, "_AGENTS", OrderedDict())
    FakeAgent.construct_count = 0
    FakeAgent.construct_delay = 0.004
    yield
    FakeAgent.construct_count = 0


def _run_concurrently(fn, count: int = THREADS):
    """启动 count 个线程同时执行 fn(i)，返回 (结果列表, 异常列表)。"""
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index: int):
        try:
            barrier.wait()
            results[index] = fn(index)
        except BaseException as exc:  # noqa: BLE001 - 测试需捕获一切
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results, errors


# ---------------------------------------------------------------- 同一用户
class TestConcurrentSameUser:
    def test_single_instance_for_same_user(self):
        """32 线程抢同一个 user：只能构造一次，且拿到同一实例。"""
        results, errors = _run_concurrently(lambda _: deps.get_agent("u-same"))

        assert errors == []
        assert FakeAgent.construct_count == 1, "同一 user 被并发构造了多个实例（竞态）"
        assert all(r is results[0] for r in results)
        assert len(deps._AGENTS) == 1

    def test_no_keyerror_under_contention(self):
        """混合读写（get + new_session）不得抛 KeyError。"""

        def work(index: int):
            if index % 3 == 0:
                return deps.new_session("u-same")
            return deps.get_agent("u-same")

        _, errors = _run_concurrently(work)
        assert errors == [], f"并发访问抛出异常: {errors[:3]}"

    def test_cached_instance_is_not_cleaned_up(self):
        """胜出实例不应被误 cleanup（说明没有被当成淘汰对象）。"""
        results, _ = _run_concurrently(lambda _: deps.get_agent("u-same"))
        assert results[0].cleaned == 0


# ---------------------------------------------------------------- 多用户 + 淘汰
class TestConcurrentDistinctUsersWithEviction:
    def test_lru_cap_respected(self, monkeypatch):
        """并发下 LRU 上限必须严格生效，且不抛异常。"""
        monkeypatch.setattr(deps, "_MAX_AGENTS", 8)
        FakeAgent.construct_delay = 0.001

        _, errors = _run_concurrently(lambda i: deps.get_agent(f"u-{i}"))

        assert errors == []
        assert len(deps._AGENTS) <= 8
        assert FakeAgent.construct_count == THREADS  # 每个 user 各构造一次

    def test_evicted_agents_are_cleaned_up(self, monkeypatch):
        """被淘汰的实例必须 cleanup 过（落盘不丢数据）。"""
        monkeypatch.setattr(deps, "_MAX_AGENTS", 4)
        FakeAgent.construct_delay = 0.0
        created = [deps.get_agent(f"u-{i}") for i in range(10)]

        survivors = set(id(a) for a in deps._AGENTS.values())
        evicted = [a for a in created if id(a) not in survivors]
        assert evicted, "应有实例被淘汰"
        assert all(a.cleaned == 1 for a in evicted)

    def test_distinct_users_get_distinct_instances(self):
        FakeAgent.construct_delay = 0.0
        a = deps.get_agent("u-a")
        b = deps.get_agent("u-b")
        assert a is not b

    def test_move_to_end_keeps_recent_user(self, monkeypatch):
        """访问过的 user 应留在缓存里（LRU 语义未被锁改变）。"""
        monkeypatch.setattr(deps, "_MAX_AGENTS", 3)
        FakeAgent.construct_delay = 0.0
        first = deps.get_agent("u-0")
        for i in range(1, 3):
            deps.get_agent(f"u-{i}")
        deps.get_agent("u-0")          # 刷新为最近使用
        deps.get_agent("u-9")          # 触发淘汰

        assert "u-0" in deps._AGENTS
        assert deps._AGENTS["u-0"] is first


# ---------------------------------------------------------------- 锁与 reset
class TestLockAndReset:
    def test_lock_is_reentrant(self):
        """必须是 RLock：new_session 会在持锁状态下嵌套调用 get_agent。"""
        assert isinstance(deps._AGENTS_LOCK, type(threading.RLock()))
        with deps._AGENTS_LOCK:
            with deps._AGENTS_LOCK:
                assert deps.get_agent("u-reentrant") is not None

    def test_new_session_returns_id_of_cached_instance(self):
        FakeAgent.construct_delay = 0.0
        session_id = deps.new_session("u-x")
        assert deps._AGENTS["u-x"].session_id == session_id

    def test_reset_agents_clears_and_cleans(self):
        FakeAgent.construct_delay = 0.0
        agents = [deps.get_agent(f"u-{i}") for i in range(3)]

        dropped = deps.reset_agents()

        assert dropped == 3
        assert len(deps._AGENTS) == 0
        assert all(a.cleaned == 1 for a in agents)

    def test_reset_agents_is_idempotent(self):
        assert deps.reset_agents() == 0

    def test_reset_agents_survives_cleanup_error(self):
        """某个实例 cleanup 抛错不应中断整体清理。"""
        FakeAgent.construct_delay = 0.0
        bad = deps.get_agent("u-bad")

        def boom():
            raise RuntimeError("cleanup failed")

        bad.cleanup = boom
        deps.get_agent("u-good")

        assert deps.reset_agents() == 2
        assert len(deps._AGENTS) == 0

    def test_concurrent_reset_and_get(self):
        """reset 与 get 并发不应抛异常，也不应留下不一致状态。"""
        FakeAgent.construct_delay = 0.001

        def work(index: int):
            if index % 5 == 0:
                return deps.reset_agents()
            return deps.get_agent(f"u-{index % 4}")

        _, errors = _run_concurrently(work)
        assert errors == []


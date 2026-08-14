"""建议记录与执行反馈存储（三期 R11，方案 §3.1–3.2）。

**为什么不复用 episodic memory**：``service.py`` 里的 ``recent_feedback`` 只是
distill 阶段的**文本分类**（把用户话语归入「近期反馈」类别），既没有「哪条建议」
的指向，也没有状态字段，**不是执行跟踪**。而 episodic 的查询接口是
``search_memories(limit=1000)`` 全捞再过滤，按 ``status`` / ``due_at`` 检索非常
别扭。建议记录是**强结构、需按状态检索**的业务数据，独立存储更干净，也便于
R12 时间线与 R10 推送直接消费。

存储形态：``logs/advice/<user_id>.jsonl``，**append-only**。

    更新语义 = 追加一条同 ``advice_id`` 的新行，读时按 ``advice_id``
    取**最后一条**。

这样避免了原地改写（读-改-写）在并发下的丢更新问题 —— R10 的 tick 线程与
HTTP 请求线程会同时碰这个文件。单行写入配合 ``O_APPEND`` 在 POSIX 上对
小于 PIPE_BUF 的写是原子的，因此不会出现半行交错。
"""

from __future__ import annotations

import json
import os
import random
import string
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional


#: 建议状态机取值。``pending`` 是初始态，其余三个是终态（仍可被再次更新）。
STATUS_PENDING = "pending"
STATUS_ADOPTED = "adopted"
STATUS_REJECTED = "rejected"
STATUS_PARTIAL = "partial"

VALID_STATUSES: frozenset[str] = frozenset(
    {STATUS_PENDING, STATUS_ADOPTED, STATUS_REJECTED, STATUS_PARTIAL}
)

DEFAULT_ADVICE_DIR = "logs/advice"
DEFAULT_FOLLOWUP_DELAY_HOURS = 24

#: 摘要在 trace / 回访注入里的截断长度。
SUMMARY_PREVIEW = 120
#: 落盘摘要上限（防止把整份食谱塞进 JSONL）。
SUMMARY_MAX = 600


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def new_advice_id() -> str:
    """生成 ``a-YYYYMMDD-HHMMSS-xxxx`` 形式的建议 id（与 SessionStore 同款风格）。"""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"a-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{suffix}"


@dataclass
class AdviceRecord:
    """一条已给出的建议及其执行状态（方案 §3.2）。"""

    advice_id: str
    user_id: str
    session_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    #: 面向回访提问的建议摘要。
    summary: str = ""
    #: 结构化要点（约束 / 天数 / 关键餐次），由 planning 子代理的 JSON 块提供。
    plan_digest: dict = field(default_factory=dict)
    status: str = STATUS_PENDING
    feedback_text: str = ""
    #: 默认 ``created_at + WELLNESS_FOLLOWUP_DELAY_HOURS``。
    followup_due_at: str = ""
    followed_up_at: str = ""
    #: 最后一次状态变更时间（供时间线排序）。
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["AdviceRecord"]:
        """从落盘行恢复；缺 ``advice_id`` 视为坏行返回 None。"""
        data = data or {}
        advice_id = str(data.get("advice_id") or "").strip()
        if not advice_id:
            return None
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        payload = {k: v for k, v in data.items() if k in known}
        payload["advice_id"] = advice_id
        payload["user_id"] = str(payload.get("user_id") or "")
        if not isinstance(payload.get("plan_digest"), dict):
            payload["plan_digest"] = {}
        status = str(payload.get("status") or STATUS_PENDING)
        payload["status"] = status if status in VALID_STATUSES else STATUS_PENDING
        return cls(**payload)

    # ------------------------------------------------------------------ derived
    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    def summary_preview(self, limit: int = SUMMARY_PREVIEW) -> str:
        """trace / 回访注入用的短摘要。"""
        return " ".join((self.summary or "").split())[:limit]

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """是否到了该回访的时间。

        判定条件（三者同时成立）：仍是 ``pending``、尚未回访过、且已过
        ``followup_due_at``。``followup_due_at`` 解析不出来时视为**未到期**——
        宁可不打扰，也不要因为脏数据把用户淹了。
        """
        if self.status != STATUS_PENDING or self.followed_up_at:
            return False
        due = _parse_iso(self.followup_due_at)
        if due is None:
            return False
        return (now or datetime.now()) >= due


def _parse_iso(value: str) -> Optional[datetime]:
    """宽松解析 ISO 时间串；失败返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class AdviceStore:
    """建议记录的 append-only JSONL 存储。

    - :meth:`append` 追加一条新建议；
    - :meth:`list` 按状态检索（读时按 ``advice_id`` 折叠为最后一条）；
    - :meth:`update_status` / :meth:`mark_followed_up` 追加更新行；
    - :meth:`due_for_followup` 取到期待回访项（R10 推送与回访注入共用）。
    """

    def __init__(
        self,
        advice_dir: Optional[str] = None,
        followup_delay_hours: Optional[int] = None,
    ):
        self.advice_dir = Path(
            advice_dir or os.getenv("WELLNESS_ADVICE_DIR", DEFAULT_ADVICE_DIR)
        )
        self.followup_delay_hours = (
            followup_delay_hours
            if followup_delay_hours is not None
            else _env_int(
                "WELLNESS_FOLLOWUP_DELAY_HOURS", DEFAULT_FOLLOWUP_DELAY_HOURS
            )
        )
        # 同进程内的写串行化。跨进程仍靠 O_APPEND 的原子小写保证不撕行。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths
    def path_for(self, user_id: str) -> Path:
        """某用户的 JSONL 路径。``user_id`` 里的路径分隔符会被剔除。"""
        safe = str(user_id or "unknown").replace("/", "_").replace("\\", "_")
        if safe in {".", ".."}:
            safe = "unknown"
        return self.advice_dir / f"{safe}.jsonl"

    # ------------------------------------------------------------------ write
    def append(self, record: AdviceRecord) -> str:
        """追加一条建议记录，返回 ``advice_id``。

        ``followup_due_at`` 为空时按 ``created_at + followup_delay_hours`` 补齐。
        """
        if not record.advice_id:
            record.advice_id = new_advice_id()
        if not record.followup_due_at:
            created = _parse_iso(record.created_at) or datetime.now()
            record.followup_due_at = (
                created + timedelta(hours=self.followup_delay_hours)
            ).isoformat()
        if not record.updated_at:
            record.updated_at = record.created_at
        record.summary = (record.summary or "")[:SUMMARY_MAX]
        self._append_line(record.user_id, record.to_dict())
        return record.advice_id

    def _append_line(self, user_id: str, payload: dict) -> None:
        """把一行 JSON 追加到用户的 JSONL（O_APPEND，单次小写原子）。"""
        path = self.path_for(user_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def update_status(
        self,
        user_id: str,
        advice_id: str,
        status: str,
        feedback_text: str = "",
    ) -> bool:
        """更新一条建议的状态（追加写）。记录不存在或状态非法返回 False。"""
        if status not in VALID_STATUSES:
            return False
        with self._lock:
            record = self.get(user_id, advice_id)
            if record is None:
                return False
            record.status = status
            if feedback_text:
                record.feedback_text = feedback_text[:SUMMARY_MAX]
            record.updated_at = datetime.now().isoformat()
            # 已给出反馈即视为回访闭环，避免继续出现在待回访队列里。
            if not record.followed_up_at:
                record.followed_up_at = record.updated_at
            self._append_line(user_id, record.to_dict())
            return True

    def mark_followed_up(self, user_id: str, advice_id: str) -> bool:
        """标记「已发起回访」（状态保持 ``pending``，等用户真正反馈）。"""
        with self._lock:
            record = self.get(user_id, advice_id)
            if record is None:
                return False
            record.followed_up_at = datetime.now().isoformat()
            record.updated_at = record.followed_up_at
            self._append_line(user_id, record.to_dict())
            return True

    # ------------------------------------------------------------------ read
    def _read_all(self, user_id: str) -> list[AdviceRecord]:
        """读全量并按 ``advice_id`` 折叠为最后一条（append-only 更新语义）。

        保持**首次出现顺序**（= 创建顺序），坏行静默跳过。
        """
        path = self.path_for(user_id)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

        order: list[str] = []
        latest: dict[str, AdviceRecord] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:  # noqa: BLE001 - 坏行不该拖垮整个查询
                continue
            record = AdviceRecord.from_dict(data)
            if record is None:
                continue
            if record.advice_id not in latest:
                order.append(record.advice_id)
            latest[record.advice_id] = record
        return [latest[aid] for aid in order]

    def get(self, user_id: str, advice_id: str) -> Optional[AdviceRecord]:
        """按 id 取最新状态的记录。"""
        if not advice_id:
            return None
        for record in self._read_all(user_id):
            if record.advice_id == advice_id:
                return record
        return None

    def list(
        self,
        user_id: str,
        status: Optional[str | Iterable[str]] = None,
        limit: int = 50,
    ) -> list[AdviceRecord]:
        """按状态检索，**最新创建的在前**。``status`` 可传单个或多个。"""
        records = self._read_all(user_id)
        if status:
            wanted = {status} if isinstance(status, str) else set(status)
            records = [r for r in records if r.status in wanted]
        records.sort(key=lambda r: (r.created_at, r.advice_id), reverse=True)
        if limit and limit > 0:
            return records[:limit]
        return records

    def due_for_followup(
        self, user_id: str, now: Optional[datetime] = None, limit: int = 5
    ) -> list[AdviceRecord]:
        """取到期待回访的建议（最早到期的在前 —— 先问最久没跟进的那条）。"""
        due = [r for r in self._read_all(user_id) if r.is_due(now)]
        due.sort(key=lambda r: (r.followup_due_at, r.advice_id))
        if limit and limit > 0:
            return due[:limit]
        return due

    def latest_pending(self, user_id: str) -> Optional[AdviceRecord]:
        """最近一条仍 ``pending`` 的建议。

        供 ``advice_feedback`` 工具在模型没给 ``advice_id`` 时兜底 —— 用户说
        「那个食谱我试了」时，模型通常不会知道内部 id。
        """
        pending = self.list(user_id, status=STATUS_PENDING, limit=1)
        return pending[0] if pending else None

    def counts_by_status(self, user_id: str) -> dict[str, int]:
        """各状态计数（供 ``/api/advice/list`` 与状态面板展示）。"""
        counts = {status: 0 for status in sorted(VALID_STATUSES)}
        for record in self._read_all(user_id):
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts


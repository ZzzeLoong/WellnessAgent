"""用户上传媒体的落盘与解析（三期 R9，方案 §2.2）。

这是让「全链路多模态」不撑爆存储的关键设计：

============================  ==========================================  ==========
层                            图片形态                                     大小量级
============================  ==========================================  ==========
前端上传                      ``multipart/form-data``                     原始
落盘                          ``<dir>/<user_id>/<sha256>.<ext>`` + ``.json``  原始
``Message.content`` / trace   ``wellness-file://<file_id>``               **~60 字节**
发往 LLM（``to_openai``）      ``data:image/jpeg;base64,...``              原始 × 1.33
============================  ==========================================  ==========

收益：

- ``SessionStore`` 每轮全量重写，存 base64 的话 10 轮带图对话就是几十 MB 反复读写；
- ``TraceLogger._sanitize`` 对每个字符串跑 4 个正则，base64 会让脱敏开销爆炸；
- 重启后 ``load_session`` 恢复 messages，图片仍在盘上 → **多模态会话可跨重启续接**。

``file_id`` 用**内容 sha256**：天然去重（同图重复上传只存一份），且可作为感知
记忆的稳定主键。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


#: mime → 扩展名白名单（方案 §2.5：jpeg / png / webp / gif）。
ALLOWED_MIME_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

#: 扩展名 → 规范 mime（按文件名兜底推断用）。
_EXT_TO_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}

DEFAULT_MEDIA_DIR = "logs/uploads"
DEFAULT_MAX_IMAGE_MB = 8
DEFAULT_TTL_DAYS = 30


class MediaStoreError(Exception):
    """媒体落盘/校验失败。"""


class MediaTooLargeError(MediaStoreError):
    """超过 ``WELLNESS_VLM_MAX_IMAGE_MB``。"""


class UnsupportedMediaTypeError(MediaStoreError):
    """mime 不在白名单内。"""


@dataclass
class MediaRef:
    """一条落盘媒体的元信息。"""

    file_id: str
    user_id: str
    mime: str
    size_bytes: int
    path: str
    filename: str = ""
    width: int = 0
    height: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaRef":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @property
    def preview_url(self) -> str:
        """前端气泡渲染用的原图地址。"""
        return f"/api/media/{self.file_id}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _sniff_mime(data: bytes) -> Optional[str]:
    """按魔数嗅探图片 mime（不信任客户端声明的 content-type）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _probe_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    """纯标准库解析图片宽高（避免为此引入 Pillow）。失败返回 ``(0, 0)``。"""
    try:
        if mime == "image/png":
            # IHDR 紧跟 8 字节签名 + 4 字节长度 + 4 字节类型。
            if data[12:16] == b"IHDR":
                w, h = struct.unpack(">II", data[16:24])
                return int(w), int(h)
        elif mime == "image/gif":
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        elif mime == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        elif mime in ("image/jpeg", "image/jpg"):
            i = 2
            total = len(data)
            while i + 9 < total:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                # SOF0..SOF15，排除 DHT(C4) / JPG(C8) / DAC(CC)。
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return int(w), int(h)
                seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
                if seg_len <= 0:
                    break
                i += 2 + seg_len
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


class MediaStore:
    """用户上传媒体的落盘与解析。

    - ``save(user_id, filename, data)`` → :class:`MediaRef`
    - ``resolve_data_url(file_id)`` → ``"data:image/jpeg;base64,..."``（发送 LLM 时才调）
    - ``path_of(file_id)`` → ``Path``（感知记忆 / 时间线用）
    """

    def __init__(
        self,
        media_dir: Optional[str] = None,
        max_image_mb: Optional[int] = None,
        ttl_days: Optional[int] = None,
    ):
        self.media_dir = Path(
            media_dir or os.getenv("WELLNESS_MEDIA_DIR", DEFAULT_MEDIA_DIR)
        )
        self.max_image_mb = (
            max_image_mb
            if max_image_mb is not None
            else _env_int("WELLNESS_VLM_MAX_IMAGE_MB", DEFAULT_MAX_IMAGE_MB)
        )
        self.ttl_days = (
            ttl_days
            if ttl_days is not None
            else _env_int("WELLNESS_MEDIA_TTL_DAYS", DEFAULT_TTL_DAYS)
        )

    # ------------------------------------------------------------------ paths
    def _user_dir(self, user_id: str) -> Path:
        return self.media_dir / user_id

    def _meta_path_for(self, media_path: Path) -> Path:
        return media_path.with_suffix(media_path.suffix + ".json")

    def _find(self, file_id: str, user_id: Optional[str] = None) -> Optional[Path]:
        """按 file_id 定位落盘文件。给定 ``user_id`` 时只在该用户目录下找。"""
        if not file_id or "/" in file_id or "\\" in file_id or ".." in file_id:
            return None
        roots: Iterable[Path]
        if user_id:
            roots = [self._user_dir(user_id)]
        elif self.media_dir.exists():
            roots = [p for p in self.media_dir.iterdir() if p.is_dir()]
        else:
            roots = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.glob(f"{file_id}.*"):
                if path.suffix == ".json":
                    continue
                return path
        return None

    # ------------------------------------------------------------------ write
    def save(
        self,
        user_id: str,
        filename: str,
        data: bytes,
        declared_mime: Optional[str] = None,
    ) -> MediaRef:
        """校验并落盘一份媒体，返回 :class:`MediaRef`。

        Raises:
            MediaStoreError: 空数据。
            MediaTooLargeError: 超过 ``max_image_mb``。
            UnsupportedMediaTypeError: mime 不在白名单。
        """
        if not data:
            raise MediaStoreError("上传内容为空。")
        limit = self.max_image_mb * 1024 * 1024
        if len(data) > limit:
            raise MediaTooLargeError(
                f"图片大小 {len(data) / 1048576:.1f}MB 超过上限 {self.max_image_mb}MB。"
            )

        mime = self._resolve_mime(data, filename, declared_mime)
        ext = ALLOWED_MIME_TYPES[mime]
        file_id = hashlib.sha256(data).hexdigest()

        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{file_id}.{ext}"

        width, height = _probe_dimensions(data, mime)
        ref = MediaRef(
            file_id=file_id,
            user_id=user_id,
            mime=mime,
            size_bytes=len(data),
            path=str(path),
            filename=filename or f"{file_id}.{ext}",
            width=width,
            height=height,
        )

        # sha256 去重：同一张图重复上传只存一份，保留最早的 created_at。
        if path.exists():
            existing = self._read_meta(path)
            if existing is not None:
                ref.created_at = existing.created_at
        else:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        self._write_meta(path, ref)
        return ref

    def _resolve_mime(
        self, data: bytes, filename: str, declared_mime: Optional[str]
    ) -> str:
        """魔数优先 → 声明 mime → 扩展名兜底；均不在白名单则报错。"""
        sniffed = _sniff_mime(data)
        if sniffed in ALLOWED_MIME_TYPES:
            return "image/jpeg" if sniffed == "image/jpg" else sniffed

        candidate = (declared_mime or "").split(";")[0].strip().lower()
        if candidate in ALLOWED_MIME_TYPES:
            return "image/jpeg" if candidate == "image/jpg" else candidate

        ext = Path(filename or "").suffix.lstrip(".").lower()
        if ext in _EXT_TO_MIME:
            return _EXT_TO_MIME[ext]

        raise UnsupportedMediaTypeError(
            f"不支持的图片类型：{declared_mime or ext or 'unknown'}。"
            f"仅支持 {', '.join(sorted(set(ALLOWED_MIME_TYPES)))}。"
        )

    def _write_meta(self, media_path: Path, ref: MediaRef) -> None:
        meta_path = self._meta_path_for(media_path)
        tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(ref.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, meta_path)

    def _read_meta(self, media_path: Path) -> Optional[MediaRef]:
        meta_path = self._meta_path_for(media_path)
        if not meta_path.exists():
            return None
        try:
            return MediaRef.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ read
    def path_of(self, file_id: str, user_id: Optional[str] = None) -> Optional[Path]:
        """返回落盘路径（感知记忆的 ``raw_data`` / 时间线缩略图用）。"""
        return self._find(file_id, user_id)

    def get(self, file_id: str, user_id: Optional[str] = None) -> Optional[MediaRef]:
        """读取元信息；文件不存在返回 None。"""
        path = self._find(file_id, user_id)
        if path is None:
            return None
        ref = self._read_meta(path)
        if ref is not None:
            return ref
        # 元信息丢失时按文件本身重建（宽高未知）。
        ext = path.suffix.lstrip(".").lower()
        try:
            size = path.stat().st_size
        except OSError:
            return None
        return MediaRef(
            file_id=file_id,
            user_id=user_id or path.parent.name,
            mime=_EXT_TO_MIME.get(ext, "application/octet-stream"),
            size_bytes=size,
            path=str(path),
            filename=path.name,
        )

    def read_bytes(self, file_id: str, user_id: Optional[str] = None) -> Optional[bytes]:
        """读原始字节（``GET /api/media/{file_id}`` 用）。"""
        path = self._find(file_id, user_id)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def resolve_data_url(
        self, file_id: str, user_id: Optional[str] = None
    ) -> Optional[str]:
        """把 file_id 还原为 ``data:<mime>;base64,<...>``。

        **只在发送给 LLM 前调用**（``Message.to_openai(resolver)`` 内）。
        文件不存在返回 ``None``，由 ``Message`` 降级为 ``[图片]`` 占位。
        """
        path = self._find(file_id, user_id)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        ext = path.suffix.lstrip(".").lower()
        mime = _EXT_TO_MIME.get(ext, "image/jpeg")
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    def make_resolver(self, user_id: Optional[str] = None):
        """返回可直接传给 ``Message.to_openai(resolver=...)`` 的回调。"""

        def _resolve(file_id: str) -> str:
            return self.resolve_data_url(file_id, user_id) or ""

        return _resolve

    def exists(self, file_id: str, user_id: Optional[str] = None) -> bool:
        return self._find(file_id, user_id) is not None

    def list_for_user(self, user_id: str) -> list[MediaRef]:
        """列出某用户全部媒体（按创建时间倒序）。"""
        user_dir = self._user_dir(user_id)
        if not user_dir.exists():
            return []
        refs: list[MediaRef] = []
        for path in user_dir.iterdir():
            if path.suffix == ".json" or path.suffix.endswith(".tmp") or path.is_dir():
                continue
            ref = self._read_meta(path) or self.get(path.stem, user_id)
            if ref is not None:
                refs.append(ref)
        refs.sort(key=lambda r: r.created_at, reverse=True)
        return refs

    # ------------------------------------------------------------------ cleanup
    def cleanup_expired(
        self,
        now: Optional[float] = None,
        keep_file_ids: Optional[Iterable[str]] = None,
    ) -> list[str]:
        """删除超过 ``ttl_days`` 且不在 ``keep_file_ids`` 中的媒体。

        由 R10 的 tick 线程顺带调用（方案 §2.2 清理策略）。``ttl_days <= 0``
        表示永不过期。返回被删除的 file_id 列表。
        """
        if self.ttl_days <= 0 or not self.media_dir.exists():
            return []
        now = now if now is not None else time.time()
        cutoff = now - self.ttl_days * 86400
        keep = set(keep_file_ids or ())
        removed: list[str] = []

        for user_dir in self.media_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for path in list(user_dir.iterdir()):
                if path.suffix == ".json" or path.is_dir():
                    continue
                file_id = path.stem
                if file_id in keep:
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    path.unlink()
                    meta = self._meta_path_for(path)
                    if meta.exists():
                        meta.unlink()
                except OSError:
                    continue
                removed.append(file_id)
        return removed


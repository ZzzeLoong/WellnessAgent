"""core/media_store.py 单元测试（三期 R9，方案 §2.2）。

覆盖：落盘 / sha256 去重 / mime 校验 / 大小上限 / data-url 解析 / TTL 清理，
以及最关键的一条红线：**base64 绝不能出现在 Message.content 里**。
"""

import base64
import json
import struct
import time
import zlib

import pytest

from core.media_store import (
    ALLOWED_MIME_TYPES,
    MediaRef,
    MediaStore,
    MediaStoreError,
    MediaTooLargeError,
    UnsupportedMediaTypeError,
)
from core.message import MEDIA_PLACEHOLDER, Message, make_image_part, make_text_part


# ------------------------------------------------------------------ 测试素材
def _png_bytes(width: int = 3, height: int = 5) -> bytes:
    """构造一张最小合法 PNG（含真实 IHDR 宽高）。"""

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _gif_bytes(width: int = 7, height: int = 11) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 10


def _jpeg_bytes(width: int = 9, height: int = 13) -> bytes:
    # SOI + SOF0 段（precision, height, width, components）。
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x00\x00\x00"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 2) + sof + b"\xff\xd9"


def _webp_bytes(width: int = 4, height: int = 6) -> bytes:
    body = b"VP8 " + struct.pack("<I", 10)
    body += b"\x00" * 6 + struct.pack("<HH", width, height)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


@pytest.fixture()
def store(tmp_path):
    return MediaStore(media_dir=str(tmp_path / "uploads"), max_image_mb=1, ttl_days=30)


# ------------------------------------------------------------------ 构造与配置
class TestMediaStoreConfig:
    def test_defaults_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WELLNESS_MEDIA_DIR", str(tmp_path / "m"))
        monkeypatch.setenv("WELLNESS_VLM_MAX_IMAGE_MB", "3")
        monkeypatch.setenv("WELLNESS_MEDIA_TTL_DAYS", "7")
        s = MediaStore()
        assert str(s.media_dir) == str(tmp_path / "m")
        assert s.max_image_mb == 3
        assert s.ttl_days == 7

    def test_explicit_args_win_over_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WELLNESS_VLM_MAX_IMAGE_MB", "3")
        s = MediaStore(media_dir=str(tmp_path), max_image_mb=9)
        assert s.max_image_mb == 9

    def test_invalid_env_falls_back_to_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WELLNESS_MEDIA_DIR", str(tmp_path))
        monkeypatch.setenv("WELLNESS_VLM_MAX_IMAGE_MB", "not-a-number")
        assert MediaStore().max_image_mb == 8

    def test_construction_does_not_create_dir(self, tmp_path):
        """构造应零副作用；目录在首次 save 时才建。"""
        target = tmp_path / "lazy"
        MediaStore(media_dir=str(target))
        assert not target.exists()


# ------------------------------------------------------------------ 落盘
class TestSave:
    def test_save_returns_ref(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert isinstance(ref, MediaRef)
        assert ref.user_id == "u1"
        assert ref.mime == "image/png"
        assert ref.size_bytes == len(_png_bytes())

    def test_file_id_is_content_sha256(self, store):
        import hashlib

        data = _png_bytes()
        ref = store.save("u1", "a.png", data)
        assert ref.file_id == hashlib.sha256(data).hexdigest()

    def test_file_written_to_disk(self, store):
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        assert Path(ref.path).exists()
        assert Path(ref.path).read_bytes() == _png_bytes()

    def test_sidecar_metadata_written(self, store):
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        meta = Path(ref.path + ".json")
        assert meta.exists()
        assert json.loads(meta.read_text())["file_id"] == ref.file_id

    def test_dedup_same_content(self, store):
        """sha256 去重：同图重复上传只存一份。"""
        data = _png_bytes()
        first = store.save("u1", "a.png", data)
        second = store.save("u1", "b.png", data)
        assert first.file_id == second.file_id
        assert len(store.list_for_user("u1")) == 1

    def test_dedup_preserves_created_at(self, store):
        data = _png_bytes()
        first = store.save("u1", "a.png", data)
        second = store.save("u1", "a.png", data)
        assert second.created_at == first.created_at

    def test_different_content_different_ids(self, store):
        a = store.save("u1", "a.png", _png_bytes(3, 5))
        b = store.save("u1", "b.png", _png_bytes(4, 6))
        assert a.file_id != b.file_id

    def test_users_are_isolated(self, store):
        data = _png_bytes()
        store.save("u1", "a.png", data)
        store.save("u2", "a.png", data)
        assert len(store.list_for_user("u1")) == 1
        assert len(store.list_for_user("u2")) == 1

    def test_empty_data_rejected(self, store):
        with pytest.raises(MediaStoreError):
            store.save("u1", "a.png", b"")

    def test_oversize_rejected(self, store):
        with pytest.raises(MediaTooLargeError):
            store.save("u1", "big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024))

    def test_oversize_message_mentions_limit(self, store):
        with pytest.raises(MediaTooLargeError, match="1MB"):
            store.save("u1", "big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024))


# ------------------------------------------------------------------ mime 判定
class TestMimeResolution:
    @pytest.mark.parametrize(
        "maker,expected",
        [
            (_png_bytes, "image/png"),
            (_gif_bytes, "image/gif"),
            (_jpeg_bytes, "image/jpeg"),
            (_webp_bytes, "image/webp"),
        ],
    )
    def test_sniffed_from_magic_bytes(self, store, maker, expected):
        assert store.save("u1", "x.bin", maker()).mime == expected

    def test_magic_beats_wrong_declared_mime(self, store):
        """不信任客户端声明：伪装成 gif 的 png 仍判为 png。"""
        ref = store.save("u1", "a.gif", _png_bytes(), declared_mime="image/gif")
        assert ref.mime == "image/png"

    def test_declared_mime_used_when_unsniffable(self, store):
        ref = store.save("u1", "x", b"\x00\x01\x02\x03", declared_mime="image/png")
        assert ref.mime == "image/png"

    def test_extension_fallback(self, store):
        assert store.save("u1", "x.webp", b"\x00\x01\x02\x03").mime == "image/webp"

    def test_unsupported_type_rejected(self, store):
        with pytest.raises(UnsupportedMediaTypeError):
            store.save("u1", "doc.pdf", b"%PDF-1.4 body", declared_mime="application/pdf")

    def test_jpg_alias_normalized(self, store):
        ref = store.save("u1", "x", b"\x00\x01\x02\x03", declared_mime="image/jpg")
        assert ref.mime == "image/jpeg"

    def test_mime_with_charset_suffix(self, store):
        ref = store.save("u1", "x", b"\x00\x01", declared_mime="image/png; charset=binary")
        assert ref.mime == "image/png"

    def test_allowed_types_cover_spec(self):
        """方案 §2.5 白名单：jpeg / png / webp / gif。"""
        assert {"image/jpeg", "image/png", "image/webp", "image/gif"} <= set(
            ALLOWED_MIME_TYPES
        )


# ------------------------------------------------------------------ 宽高探测
class TestDimensionProbe:
    def test_png_dimensions(self, store):
        ref = store.save("u1", "a.png", _png_bytes(13, 21))
        assert (ref.width, ref.height) == (13, 21)

    def test_gif_dimensions(self, store):
        ref = store.save("u1", "a.gif", _gif_bytes(7, 11))
        assert (ref.width, ref.height) == (7, 11)

    def test_jpeg_dimensions(self, store):
        ref = store.save("u1", "a.jpg", _jpeg_bytes(9, 13))
        assert (ref.width, ref.height) == (9, 13)

    def test_webp_dimensions(self, store):
        ref = store.save("u1", "a.webp", _webp_bytes(4, 6))
        assert (ref.width, ref.height) == (4, 6)

    def test_unprobeable_degrades_to_zero(self, store):
        ref = store.save("u1", "x.png", b"\x00\x01\x02\x03", declared_mime="image/png")
        assert (ref.width, ref.height) == (0, 0)


# ------------------------------------------------------------------ 读取
class TestRead:
    def test_path_of(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert str(store.path_of(ref.file_id)) == ref.path

    def test_path_of_scoped_by_user(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert store.path_of(ref.file_id, "u1") is not None
        assert store.path_of(ref.file_id, "u2") is None

    def test_path_of_missing(self, store):
        assert store.path_of("nope") is None

    @pytest.mark.parametrize("evil", ["../etc/passwd", "a/b", "a\\b", ".."])
    def test_path_traversal_blocked(self, store, evil):
        assert store.path_of(evil) is None

    def test_get_returns_metadata(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        got = store.get(ref.file_id)
        assert got is not None and got.file_id == ref.file_id and got.width == 3

    def test_get_rebuilds_when_meta_missing(self, store):
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        Path(ref.path + ".json").unlink()
        got = store.get(ref.file_id, "u1")
        assert got is not None and got.mime == "image/png"

    def test_read_bytes_roundtrip(self, store):
        data = _png_bytes()
        ref = store.save("u1", "a.png", data)
        assert store.read_bytes(ref.file_id) == data

    def test_read_bytes_missing(self, store):
        assert store.read_bytes("nope") is None

    def test_exists(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert store.exists(ref.file_id) is True
        assert store.exists("nope") is False

    def test_preview_url(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert ref.preview_url == f"/api/media/{ref.file_id}"

    def test_list_for_user_empty(self, store):
        assert store.list_for_user("ghost") == []

    def test_list_excludes_sidecar_json(self, store):
        store.save("u1", "a.png", _png_bytes(3, 5))
        store.save("u1", "b.png", _png_bytes(4, 6))
        refs = store.list_for_user("u1")
        assert len(refs) == 2
        assert all(not r.path.endswith(".json") for r in refs)


# ------------------------------------------------------------------ data-url
class TestResolveDataUrl:
    def test_resolves_to_data_url(self, store):
        data = _png_bytes()
        ref = store.save("u1", "a.png", data)
        url = store.resolve_data_url(ref.file_id)
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == data

    def test_jpeg_mime_in_data_url(self, store):
        ref = store.save("u1", "a.jpg", _jpeg_bytes())
        assert store.resolve_data_url(ref.file_id).startswith("data:image/jpeg;base64,")

    def test_missing_returns_none(self, store):
        assert store.resolve_data_url("nope") is None

    def test_make_resolver_returns_url(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert store.make_resolver("u1")(ref.file_id).startswith("data:image/png")

    def test_make_resolver_missing_returns_empty(self, store):
        """空串让 Message 降级为 [图片] 占位，而非整轮失败。"""
        assert store.make_resolver("u1")("nope") == ""


# ------------------------------------------------------------------ 与 Message 集成
class TestMessageIntegration:
    """§2.2 红线：base64 只在发往 LLM 那一刻出现，绝不进 content。"""

    def test_content_holds_only_short_ref(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        msg = Message([make_text_part("这是什么"), make_image_part(ref.file_id)], "user")
        serialized = json.dumps(msg.to_dict(), ensure_ascii=False)
        assert "base64" not in serialized
        assert "data:image" not in serialized
        assert ref.file_id in serialized

    def test_resolver_expands_only_at_send_time(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        msg = Message([make_image_part(ref.file_id)], "user")
        payload = msg.to_openai(resolver=store.make_resolver("u1"))
        assert payload["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        # 原 content 未被污染。
        assert "base64" not in json.dumps(msg.to_dict())

    def test_deleted_file_degrades_to_placeholder(self, store):
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        msg = Message([make_image_part(ref.file_id)], "user")
        Path(ref.path).unlink()
        payload = msg.to_openai(resolver=store.make_resolver("u1"))
        assert payload["content"] == [{"type": "text", "text": MEDIA_PLACEHOLDER}]

    def test_session_payload_stays_far_below_512kb(self, store):
        """§10.2 CI 断言口径：整个 session JSON 单文件应 < 512 KB。"""
        refs = [store.save("u1", f"{i}.png", _png_bytes(3, 5 + i)) for i in range(10)]
        messages = [
            Message([make_text_part("这盘多少卡"), make_image_part(r.file_id)], "user")
            for r in refs
        ]
        payload = json.dumps([m.to_dict() for m in messages], ensure_ascii=False)
        assert len(payload.encode("utf-8")) < 512 * 1024
        assert "base64" not in payload


# ------------------------------------------------------------------ TTL 清理
class TestCleanupExpired:
    def test_fresh_files_kept(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        assert store.cleanup_expired() == []
        assert store.exists(ref.file_id)

    def test_expired_files_removed(self, store, tmp_path):
        import os as _os
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        old = time.time() - 40 * 86400
        _os.utime(Path(ref.path), (old, old))
        assert store.cleanup_expired() == [ref.file_id]
        assert not store.exists(ref.file_id)

    def test_expired_sidecar_also_removed(self, store):
        import os as _os
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        old = time.time() - 40 * 86400
        _os.utime(Path(ref.path), (old, old))
        store.cleanup_expired()
        assert not Path(ref.path + ".json").exists()

    def test_keep_file_ids_protects_referenced(self, store):
        import os as _os
        from pathlib import Path

        ref = store.save("u1", "a.png", _png_bytes())
        old = time.time() - 40 * 86400
        _os.utime(Path(ref.path), (old, old))
        assert store.cleanup_expired(keep_file_ids=[ref.file_id]) == []
        assert store.exists(ref.file_id)

    def test_ttl_zero_disables_cleanup(self, tmp_path):
        import os as _os
        from pathlib import Path

        s = MediaStore(media_dir=str(tmp_path / "u"), ttl_days=0)
        ref = s.save("u1", "a.png", _png_bytes())
        old = time.time() - 9999 * 86400
        _os.utime(Path(ref.path), (old, old))
        assert s.cleanup_expired() == []
        assert s.exists(ref.file_id)

    def test_cleanup_on_missing_dir_is_noop(self, tmp_path):
        assert MediaStore(media_dir=str(tmp_path / "absent")).cleanup_expired() == []

    def test_cleanup_respects_injected_now(self, store):
        ref = store.save("u1", "a.png", _png_bytes())
        future = time.time() + 40 * 86400
        assert store.cleanup_expired(now=future) == [ref.file_id]


# ------------------------------------------------------------------ MediaRef
class TestMediaRef:
    def test_to_dict_roundtrip(self):
        ref = MediaRef(
            file_id="f1", user_id="u1", mime="image/png", size_bytes=10, path="/tmp/a.png"
        )
        assert MediaRef.from_dict(ref.to_dict()).file_id == "f1"

    def test_from_dict_ignores_unknown_keys(self):
        ref = MediaRef.from_dict(
            {"file_id": "f1", "user_id": "u", "mime": "image/png",
             "size_bytes": 1, "path": "/x", "bogus": 1}
        )
        assert ref.file_id == "f1"


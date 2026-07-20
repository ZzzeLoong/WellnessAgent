"""tools/response.py 单元测试。"""

from tools.response import ToolResponse, ToolStatus, ToolErrorCode


class TestToolStatus:
    def test_values(self):
        assert ToolStatus.SUCCESS.value == "success"
        assert ToolStatus.PARTIAL.value == "partial"
        assert ToolStatus.ERROR.value == "error"

    def test_string_comparison(self):
        assert ToolStatus.SUCCESS == "success"
        assert ToolStatus.ERROR == "error"


class TestToolErrorCode:
    def test_known_codes(self):
        assert ToolErrorCode.EXECUTION_ERROR == "EXECUTION_ERROR"
        assert ToolErrorCode.INVALID_PARAM == "INVALID_PARAM"
        assert ToolErrorCode.NOT_FOUND == "NOT_FOUND"
        assert ToolErrorCode.CIRCUIT_OPEN == "CIRCUIT_OPEN"
        assert ToolErrorCode.TIMEOUT == "TIMEOUT"


class TestToolResponseSuccess:
    def test_success_factory(self):
        r = ToolResponse.success("done", data={"k": 1})
        assert r.status == ToolStatus.SUCCESS
        assert r.text == "done"
        assert r.data == {"k": 1}
        assert r.error_info is None
        assert r.is_success is True
        assert r.is_error is False

    def test_success_with_stats(self):
        r = ToolResponse.success("ok", stats={"latency_ms": 50})
        assert r.stats == {"latency_ms": 50}

    def test_success_default_data(self):
        r = ToolResponse.success("ok")
        assert r.data == {}


class TestToolResponsePartial:
    def test_partial_factory(self):
        r = ToolResponse.partial("some results")
        assert r.status == ToolStatus.PARTIAL
        assert r.text == "some results"
        assert r.is_success is False
        assert r.is_error is False

    def test_partial_with_data(self):
        r = ToolResponse.partial("incomplete", data={"partial": True})
        assert r.data == {"partial": True}


class TestToolResponseError:
    def test_error_factory(self):
        r = ToolResponse.error("TIMEOUT", "tool timed out")
        assert r.status == ToolStatus.ERROR
        assert r.is_error is True
        assert r.is_success is False
        assert r.error_info == {"code": "TIMEOUT", "message": "tool timed out"}
        assert "TIMEOUT" in r.text
        assert "tool timed out" in r.text

    def test_error_text_format(self):
        r = ToolResponse.error("EXECUTION_ERROR", "crash")
        assert r.text == "错误[EXECUTION_ERROR]：crash"

    def test_error_with_stats(self):
        r = ToolResponse.error("TIMEOUT", "slow", stats={"retries": 3})
        assert r.stats == {"retries": 3}


class TestToolResponseToDict:
    def test_success_to_dict(self):
        r = ToolResponse.success("done", data={"k": 1}, stats={"ms": 10})
        d = r.to_dict()
        assert d["status"] == "success"
        assert d["text"] == "done"
        assert d["data"] == {"k": 1}
        assert d["error_info"] is None
        assert d["stats"] == {"ms": 10}

    def test_error_to_dict(self):
        r = ToolResponse.error("NOT_FOUND", "missing")
        d = r.to_dict()
        assert d["status"] == "error"
        assert d["error_info"]["code"] == "NOT_FOUND"


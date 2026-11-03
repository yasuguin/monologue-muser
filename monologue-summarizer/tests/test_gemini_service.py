import gemini_service
import google.auth.exceptions
import httpx
import pytest
from google.genai import errors
from schema import SummaryResponse, TranscriptionResponse


class _FakeResponse:
    def __init__(self, parsed=None, text=None):
        self.parsed = parsed
        self.text = text


class _FakeFiles:
    def __init__(self):
        self.calls = []

    def upload(self, file, config):
        self.calls.append({"file": file, "config": config})
        return f"uploaded:{file}"


class _FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"contents": contents, "schema": config["response_schema"]})
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.files = _FakeFiles()
        self.models = _FakeModels(responses)


def _install_client(monkeypatch, responses):
    client = _FakeClient(responses)
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    return client


_AUDIO = object()
_URI = "gs://bucket/memo.m4a"


class TestTranscribeAndSummarize:
    def test_ファイルオブジェクトはMIMEタイプを指定してアップロードする(self, monkeypatch):
        # ファイルオブジェクトでは SDK が MIME タイプを推定できない。
        transcription = TranscriptionResponse(transcription="t")
        summary = SummaryResponse(markdown="m", nextActions=[], tags=["t"])
        client = _install_client(monkeypatch, [_FakeResponse(parsed=transcription),
                                               _FakeResponse(parsed=summary)])

        gemini_service.transcribe_and_summarize(_AUDIO, "audio/aiff", _URI)

        assert client.files.calls == [{"file": _AUDIO, "config": {"mime_type": "audio/aiff"}}]

    def test_returns_markdown_next_actions_and_tags_on_success(self, monkeypatch):
        transcription = TranscriptionResponse(transcription="これは独り言です")
        summary = SummaryResponse(
            markdown="### 話題\n本文", nextActions=["やる"], tags=["AI"]
        )
        _install_client(monkeypatch, [_FakeResponse(parsed=transcription),
                                      _FakeResponse(parsed=summary)])

        result = gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

        assert result == {"markdown": "### 話題\n本文", "nextActions": ["やる"], "tags": ["AI"]}

    def test_transcription_falls_back_to_text_when_parsed_is_none(self, monkeypatch):
        summary = SummaryResponse(markdown="m", nextActions=[], tags=["t"])
        client = _install_client(
            monkeypatch,
            [_FakeResponse(parsed=None, text="素のテキスト"), _FakeResponse(parsed=summary)],
        )

        gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

        # 2 回目（要約）のプロンプトにフォールバックした文字起こしが渡る。
        assert "素のテキスト" in client.models.calls[1]["contents"]

    def test_returns_symmetric_keys_on_transcription_error(self, monkeypatch):
        client = _FakeClient([])
        client.files.upload = lambda file, config: (_ for _ in ()).throw(RuntimeError("boom"))
        monkeypatch.setattr(gemini_service, "_get_client", lambda: client)

        with pytest.raises(RuntimeError, match="boom"):
            gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

    def test_returns_symmetric_keys_on_summary_error(self, monkeypatch):
        transcription = TranscriptionResponse(transcription="t")
        client = _FakeClient([_FakeResponse(parsed=transcription)])
        original = client.models.generate_content
        calls = {"n": 0}

        def flaky(model, contents, config):
            calls["n"] += 1
            if calls["n"] == 1:
                return original(model, contents, config)
            raise RuntimeError("summary boom")

        client.models.generate_content = flaky
        monkeypatch.setattr(gemini_service, "_get_client", lambda: client)

        with pytest.raises(RuntimeError, match="summary boom"):
            gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)


def test_文字起こし失敗は再試行できるよう例外を伝える(monkeypatch):
    import pytest

    client = _FakeClient([])
    client.files.upload = lambda file, config: (_ for _ in ()).throw(RuntimeError("boom"))
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    with pytest.raises(RuntimeError, match="boom"):
        gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)


def _api_error(cls, code, message="boom", status="INTERNAL"):
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


def _quota_exhausted():
    return _api_error(errors.ClientError, 429, "quota", "RESOURCE_EXHAUSTED")


class _RaisingModels:
    """呼ばれるたびに次の値を返す。例外なら投げる。"""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"contents": contents, "schema": config["response_schema"]})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_clients(monkeypatch, free_outcomes, vertex_outcomes):
    free = _FakeClient([])
    free.models = _RaisingModels(free_outcomes)
    vertex = _FakeClient([])
    vertex.models = _RaisingModels(vertex_outcomes)
    monkeypatch.setattr(gemini_service, "_get_client", lambda: free)
    monkeypatch.setattr(gemini_service, "_get_vertex_client", lambda: vertex)
    return free, vertex


@pytest.fixture(autouse=True)
def _無料枠から試す状態に戻す(monkeypatch):
    monkeypatch.setattr(gemini_service, "_prefer_agent_platform", False)


_TRANSCRIPTION = TranscriptionResponse(transcription="これは独り言です")
_SUMMARY = SummaryResponse(markdown="m", nextActions=["やる"], tags=["t"])
_RESULT = {"markdown": "m", "nextActions": ["やる"], "tags": ["t"]}


class TestFallbackToAgentPlatform:
    def test_無料枠の上限で文字起こしをGCSの音声参照でやり直す(self, monkeypatch):
        _, vertex = _install_clients(
            monkeypatch,
            free_outcomes=[_quota_exhausted()],
            vertex_outcomes=[_FakeResponse(parsed=_TRANSCRIPTION), _FakeResponse(parsed=_SUMMARY)],
        )

        result = gemini_service.transcribe_and_summarize(_AUDIO, "audio/aiff", _URI)

        # Agent Platform では Files API が使えないので、GCS の音声を URI で渡す。
        audio_part = vertex.models.calls[0]["contents"][1]
        assert (audio_part.file_data.file_uri, audio_part.file_data.mime_type) == (
            _URI, "audio/aiff"
        )
        assert result == _RESULT

    def test_無料枠の上限で要約をAgentPlatformでやり直す(self, monkeypatch):
        _, vertex = _install_clients(
            monkeypatch,
            free_outcomes=[_FakeResponse(parsed=_TRANSCRIPTION), _quota_exhausted()],
            vertex_outcomes=[_FakeResponse(parsed=_SUMMARY)],
        )

        result = gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

        assert "これは独り言です" in vertex.models.calls[0]["contents"]
        assert result == _RESULT

    @pytest.mark.parametrize(
        "error",
        [
            _api_error(errors.ClientError, 403, status="PERMISSION_DENIED"),
            _api_error(errors.ServerError, 500),
            _api_error(errors.ServerError, 503, status="UNAVAILABLE"),
            # ステータスが 400 でも quota の文言があれば上限とみなす。
            _api_error(errors.ClientError, 400, "Quota exceeded for metric", "INVALID_ARGUMENT"),
            httpx.ConnectError("connection refused"),
        ],
    )
    def test_経路を変えれば直り得る失敗はAgentPlatformでやり直す(self, monkeypatch, error):
        # 上限とみなした失敗なら要約も Agent Platform から呼ぶので、
        # どちらの経路にも要約の応答を用意する。
        _, vertex = _install_clients(
            monkeypatch,
            free_outcomes=[error, _FakeResponse(parsed=_SUMMARY)],
            vertex_outcomes=[_FakeResponse(parsed=_TRANSCRIPTION),
                             _FakeResponse(parsed=_SUMMARY)],
        )

        assert gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI) == _RESULT
        assert vertex.models.calls[0]["schema"] is TranscriptionResponse

    def test_要約の応答を解釈できなければAgentPlatformでやり直す(self, monkeypatch):
        _, vertex = _install_clients(
            monkeypatch,
            free_outcomes=[_FakeResponse(parsed=_TRANSCRIPTION), _FakeResponse(parsed=None)],
            vertex_outcomes=[_FakeResponse(parsed=_SUMMARY)],
        )

        assert gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI) == _RESULT
        assert len(vertex.models.calls) == 1

    @pytest.mark.parametrize(
        "error",
        [
            _api_error(errors.ClientError, 400, "invalid argument", "INVALID_ARGUMENT"),
            _api_error(errors.ClientError, 404, "model not found", "NOT_FOUND"),
        ],
    )
    def test_経路を変えても直らない失敗はやり直さずに伝える(self, monkeypatch, error):
        _, vertex = _install_clients(monkeypatch, free_outcomes=[error], vertex_outcomes=[])

        with pytest.raises(errors.ClientError):
            gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

        assert vertex.models.calls == []

    def test_上限を一度踏んだら同じinstanceの次の呼び出しはAgentPlatformから始める(
        self, monkeypatch
    ):
        free, vertex = _install_clients(
            monkeypatch,
            free_outcomes=[_quota_exhausted()],
            vertex_outcomes=[_FakeResponse(parsed=_TRANSCRIPTION), _FakeResponse(parsed=_SUMMARY)],
        )

        gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

        # 要約は無料枠を試さずに Agent Platform で呼ぶ。
        assert len(free.models.calls) == 1
        assert len(vertex.models.calls) == 2

    def test_AgentPlatformが先でも失敗したら無料枠でやり直す(self, monkeypatch):
        monkeypatch.setattr(gemini_service, "_prefer_agent_platform", True)
        free, _ = _install_clients(
            monkeypatch,
            free_outcomes=[_FakeResponse(parsed=_TRANSCRIPTION), _FakeResponse(parsed=_SUMMARY)],
            vertex_outcomes=[_api_error(errors.ServerError, 503, status="UNAVAILABLE"),
                             _api_error(errors.ServerError, 503, status="UNAVAILABLE")],
        )

        assert gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI) == _RESULT
        assert free.files.calls == [{"file": _AUDIO, "config": {"mime_type": "audio/mp4"}}]

    def test_AgentPlatformの資格情報が取れなければ失敗として扱う(self, monkeypatch):
        free = _FakeClient([])
        free.models = _RaisingModels([_quota_exhausted()])
        monkeypatch.setattr(gemini_service, "_get_client", lambda: free)

        def no_credentials():
            raise google.auth.exceptions.DefaultCredentialsError("no credentials")

        monkeypatch.setattr(gemini_service, "_get_vertex_client", no_credentials)

        with pytest.raises(google.auth.exceptions.DefaultCredentialsError):
            gemini_service.transcribe_and_summarize(_AUDIO, "audio/mp4", _URI)

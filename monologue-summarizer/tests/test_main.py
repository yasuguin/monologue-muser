import types

import config
import main
import pytest


def _cloud_event(bucket="bucket", name="20240101_abcd1234_メモ.m4a", generation="123"):
    return types.SimpleNamespace(data={
        "bucket": bucket, "name": name, "generation": generation, "contentType": "audio/mp4",
    })


class _FakeAudio:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def _patch_pipeline(monkeypatch, *, start=True, download=True, summary=None,
                    notion=True, recorder=None):
    recorder = recorder if recorder is not None else {}
    summary = summary or {"markdown": "m", "nextActions": [], "tags": ["t"]}

    monkeypatch.setattr(main, "generate_event_id", lambda *a: "eid")
    monkeypatch.setattr(main, "try_start_processing", lambda *a: start)

    def open_audio(*a):
        if not download:
            raise RuntimeError("GCSからのファイルダウンロード中にエラーが発生しました")
        return _FakeAudio()

    monkeypatch.setattr(main, "open_file_from_gcs", open_audio)
    monkeypatch.setattr(main, "transcribe_and_summarize", lambda *a: summary)
    monkeypatch.setattr(main, "send_to_notion", lambda *a: notion)
    monkeypatch.setattr(main, "delete_file_from_gcs", lambda *a: True)
    monkeypatch.setattr(main, "mark_processing_completed", lambda *a: True)

    def failed(*a):
        recorder["failed"] = True
        return True

    monkeypatch.setattr(main, "mark_processing_failed", failed)
    return recorder


class TestSummarizeMonologue:
    def test_skips_when_already_processing(self, monkeypatch):
        rec = _patch_pipeline(monkeypatch, start=False)
        result = main.monologue_summarizer(_cloud_event())
        assert result == "Event already processed or in progress"
        assert "failed" not in rec

    def test_happy_path_completes(self, monkeypatch):
        rec = _patch_pipeline(monkeypatch)
        result = main.monologue_summarizer(_cloud_event())
        assert result == "処理が正常に完了しました"
        assert "failed" not in rec

    def test_download_failure_marks_failed_for_retry(self, monkeypatch):
        rec = _patch_pipeline(monkeypatch, download=False)
        with pytest.raises(RuntimeError, match="ダウンロード"):
            main.monologue_summarizer(_cloud_event())
        assert rec.get("failed") is True

    def test_notion_failure_marks_failed_for_retry(self, monkeypatch):
        rec = _patch_pipeline(monkeypatch, notion=False)
        with pytest.raises(RuntimeError, match="Notion"):
            main.monologue_summarizer(_cloud_event())
        assert rec.get("failed") is True

    def test_unexpected_exception_marks_failed_for_retry(self, monkeypatch):
        rec = _patch_pipeline(monkeypatch)

        def boom(*a):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(main, "transcribe_and_summarize", boom)
        with pytest.raises(RuntimeError, match="kaboom"):
            main.monologue_summarizer(_cloud_event())
        assert rec.get("failed") is True

    def test_GCSの音声を溜めずにイベントのMIMEタイプでGeminiへ渡す(self, monkeypatch):
        # 368MB の AIFF を /tmp（Cloud Run ではメモリ）に落として 512Mi を超えた。
        _patch_pipeline(monkeypatch)
        audio = _FakeAudio()
        monkeypatch.setattr(main, "open_file_from_gcs", lambda *a: audio)
        received = {}

        def transcribe(file, mime_type, gcs_uri):
            received.update(file=file, mime_type=mime_type, gcs_uri=gcs_uri, closed=file.closed)
            return {"markdown": "m", "nextActions": [], "tags": ["t"]}

        monkeypatch.setattr(main, "transcribe_and_summarize", transcribe)
        main.monologue_summarizer(_cloud_event())
        # gcs_uri は無料枠を使い切ったときに Agent Platform へ音声を渡す経路。
        assert received == {
            "file": audio, "mime_type": "audio/mp4",
            "gcs_uri": "gs://bucket/20240101_abcd1234_メモ.m4a", "closed": False,
        }
        assert audio.closed


class TestRequiredEnv:
    def test_必須envが欠けていると例外になり_try_start_processingを呼ばない(self, monkeypatch):
        _patch_pipeline(monkeypatch)
        monkeypatch.delenv("NOTION_API_KEY")
        called = {}
        monkeypatch.setattr(main, "try_start_processing", lambda *a: called.setdefault("x", True))

        with pytest.raises(config.MissingEnvError, match="NOTION_API_KEY"):
            main.monologue_summarizer(_cloud_event())
        assert "x" not in called


def test_処理失敗は再配信のため例外を返す(monkeypatch):
    _patch_pipeline(monkeypatch, download=False)
    with pytest.raises(RuntimeError, match="ダウンロード"):
        main.monologue_summarizer(_cloud_event())


def test_完了記録後の削除例外では再処理しない(monkeypatch):
    rec = _patch_pipeline(monkeypatch)

    def fail(*args):
        raise RuntimeError("削除失敗")

    monkeypatch.setattr(main, "delete_file_from_gcs", fail)
    with pytest.raises(RuntimeError, match="削除失敗"):
        main.monologue_summarizer(_cloud_event())
    assert "failed" not in rec


def test_イベントの世代を識別とダウンロードと削除に使う(monkeypatch):
    _patch_pipeline(monkeypatch)
    calls = {}
    monkeypatch.setattr(main, "generate_event_id", lambda *a: calls.setdefault("id", a) and "eid")
    monkeypatch.setattr(
        main, "open_file_from_gcs", lambda *a: calls.setdefault("download", a) and _FakeAudio()
    )
    monkeypatch.setattr(main, "delete_file_from_gcs", lambda *a: calls.setdefault("delete", a))
    main.monologue_summarizer(_cloud_event(generation="123"))
    assert calls["id"][-1] == "123"
    assert calls["download"][-1] == 123
    assert calls["delete"][-1] == 123

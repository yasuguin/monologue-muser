import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import main
import pytest


def test_失敗イベントをDiscordに通知する(monkeypatch):
    sent = {}
    monkeypatch.setenv("DISCORD_NOTIFY_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_NOTIFY_CHANNEL_ID", "123")
    monkeypatch.setattr(main, "_processing_status", lambda data: None, raising=False)

    def post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(main.requests, "post", post)
    message = base64.b64encode(json.dumps({
        "bucket": "b", "name": "a.m4a", "generation": "42",
    }).encode()).decode()
    main.monologue_notifier(SimpleNamespace(data={"message": {"data": message}}))
    assert "123/messages" in sent["url"]
    assert "a.m4a" in sent["json"]["content"]


def test_Discordへの送信失敗は再配信のため例外を返す(monkeypatch):
    monkeypatch.setenv("DISCORD_NOTIFY_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_NOTIFY_CHANNEL_ID", "123")
    monkeypatch.setattr(main, "_processing_status", lambda data: None, raising=False)

    def fail(*args, **kwargs):
        raise RuntimeError("送信失敗")

    monkeypatch.setattr(main.requests, "post", fail)
    data = base64.b64encode(json.dumps({
        "bucket": "b", "name": "a", "generation": "42",
    }).encode()).decode()
    with pytest.raises(RuntimeError, match="送信失敗"):
        main.monologue_notifier(SimpleNamespace(data={"message": {"data": data}}))


def test_イベント形式が不明な場合は失敗通知を捏造せず再配信する(monkeypatch):
    def unexpected_post(*args, **kwargs):
        pytest.fail("不明なイベントを通知しようとした")

    monkeypatch.setattr(main.requests, "post", unexpected_post)
    data = base64.b64encode(json.dumps({"unknown": "payload"}).encode()).decode()
    with pytest.raises(KeyError):
        main.monologue_notifier(SimpleNamespace(data={"message": {"data": data}}))


def test_処理中は通知を保留し完了済みなら通知しない(monkeypatch):
    message = base64.b64encode(json.dumps({
        "bucket": "b", "name": "a", "generation": "42",
    }).encode()).decode()
    event = SimpleNamespace(data={"message": {"data": message}})
    monkeypatch.setattr(main, "_processing_status", lambda data: {
        "status": "processing", "started_at": datetime.now(UTC) - timedelta(minutes=1),
    }, raising=False)
    with pytest.raises(RuntimeError, match="処理中"):
        main.monologue_notifier(event)
    monkeypatch.setattr(main, "_processing_status", lambda data: {"status": "completed"})
    assert main.monologue_notifier(event) is None


def test_元イベントの世代に対応する処理記録を参照する(monkeypatch):
    seen = {}

    class Document:
        def get(self):
            return SimpleNamespace(exists=True, to_dict=lambda: {"status": "completed"})

    class Collection:
        def document(self, key):
            seen["key"] = key
            return Document()

    class Client:
        def collection(self, name):
            seen["collection"] = name
            return Collection()

    monkeypatch.setenv("COLLECTION_NAME", "records")
    monkeypatch.setattr(main.firestore, "Client", Client)
    assert main._processing_status({"bucket": "b", "name": "a", "generation": "42"}) == {
        "status": "completed"
    }
    assert seen == {"collection": "records", "key": "eb850a6914cbb5c8695575b8a66e6e3f"}


def test_期限切れで完了記録がない場合は通知する(monkeypatch):
    sent = []
    monkeypatch.setenv("DISCORD_NOTIFY_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_NOTIFY_CHANNEL_ID", "123")
    monkeypatch.setattr(main, "_processing_status", lambda data: {
        "status": "processing", "started_at": datetime.now(UTC) - timedelta(minutes=11),
    })
    monkeypatch.setattr(main.requests, "post", lambda *a, **kw: (
        sent.append(kw["json"]["content"]) or SimpleNamespace(raise_for_status=lambda: None)
    ))
    payload = {"bucket": "b", "name": "a.m4a", "generation": "42"}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    main.monologue_notifier(SimpleNamespace(data={"message": {"data": data}}))
    assert len(sent) == 1
    assert "完了していません" in sent[0]


def test_CloudEventのdataにある音声情報で通知する(monkeypatch):
    sent = []
    monkeypatch.setenv("DISCORD_NOTIFY_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_NOTIFY_CHANNEL_ID", "123")
    monkeypatch.setattr(main, "_processing_status", lambda data: None)
    monkeypatch.setattr(main.requests, "post", lambda *a, **kw: (
        sent.append(kw["json"]["content"]) or SimpleNamespace(raise_for_status=lambda: None)
    ))
    payload = {"data": {"bucket": "b", "name": "a.m4a", "generation": "42"}}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    main.monologue_notifier(SimpleNamespace(data={"message": {"data": data}}))
    assert "a.m4a" in sent[0]


def test_実環境で確認したStorageデッドレターを処理する(monkeypatch):
    sent = []
    monkeypatch.setenv("COLLECTION_NAME", "records")
    monkeypatch.setenv("DISCORD_NOTIFY_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_NOTIFY_CHANNEL_ID", "123")

    class Document:
        def get(self):
            return SimpleNamespace(exists=False)

    class Collection:
        def document(self, key):
            assert key == "eb850a6914cbb5c8695575b8a66e6e3f"
            return Document()

    class Client:
        def collection(self, name):
            assert name == "records"
            return Collection()

    monkeypatch.setattr(main.firestore, "Client", Client)
    monkeypatch.setattr(main.requests, "post", lambda *a, **kw: (
        sent.append(kw["json"]["content"]) or SimpleNamespace(raise_for_status=lambda: None)
    ))
    # 実際の Storage 通知と同じ JSON_API_V1 の形。値は試験用に置き換える。
    payload = {"kind": "storage#object", "bucket": "b", "name": "a", "generation": "42"}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    event = SimpleNamespace(data={"message": {"data": encoded, "attributes": {
        "CloudPubSubDeadLetterSourceDeliveryCount": "5", "payloadFormat": "JSON_API_V1",
    }}})
    main.monologue_notifier(event)
    assert "バケット: b\nファイル: a" in sent[0]

"""再試行を終えた音声処理を Discord へ通知する。"""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import functions_framework
import requests
from google.cloud import firestore


def _processing_status(data):
    key = f'{data["bucket"]}/{data["name"]}/{int(data["generation"])}'
    event_id = hashlib.md5(key.encode()).hexdigest()
    snapshot = firestore.Client().collection(os.environ["COLLECTION_NAME"]).document(event_id).get()
    return snapshot.to_dict() if snapshot.exists else None


@functions_framework.cloud_event
def monologue_notifier(event):
    encoded = event.data["message"]["data"]
    payload = json.loads(base64.b64decode(encoded))
    data = payload.get("data", payload)
    name = data["name"]
    bucket = data["bucket"]
    status = _processing_status(data)
    if status and status.get("status") == "completed":
        return
    if status and status.get("status") == "processing":
        started = status.get("started_at")
        if started and datetime.now(UTC) - started < timedelta(minutes=10):
            raise RuntimeError("音声処理中のため通知を保留します")
    token = os.environ["DISCORD_NOTIFY_BOT_TOKEN"]
    channel = os.environ["DISCORD_NOTIFY_CHANNEL_ID"]
    response = requests.post(
        f"https://discord.com/api/v10/channels/{channel}/messages",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/yasu-888/monologue-muser, 1.0)",
        },
        json={
            "content": (
                f"[monologue-muser] 音声処理が再試行後も完了していません。\n"
                f"バケット: {bucket}\nファイル: {name}"
            )[:2000],
            "allowed_mentions": {"parse": []},
        },
        timeout=10,
    )
    response.raise_for_status()

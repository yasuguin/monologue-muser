import hashlib
from datetime import UTC, datetime, timedelta

import config
from google.cloud import firestore

# firestore.Client は import 時ではなく初回利用時に生成する。
# 認証なしのテスト環境で import できること、および Cloud Run のコールドスタートで
# 不要な初期化を遅延させることが目的。モジュールグローバルにキャッシュして使い回す。
_db: firestore.Client | None = None


class ProcessingInProgress(RuntimeError):
    """処理中のため再配信を待つ。"""


class ClaimLost(RuntimeError):
    """処理の所有権が別の実行へ移った。"""


def _check_owner(data: dict, owner: str) -> None:
    if data.get("owner") != owner:
        raise ClaimLost("処理の所有権を失いました")


def _check_existing_claim(data: dict, now: datetime) -> bool:
    if data.get("status") == "completed":
        return False
    started = data.get("started_at")
    if started is not None and now - started < timedelta(minutes=10):
        raise ProcessingInProgress("別の処理が進行中です")
    return True


def _get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def generate_event_id(bucket_name: str, file_name: str, event_time: str | None = None) -> str:
    content = f"{bucket_name}/{file_name}"
    if event_time:
        content += f"/{event_time}"

    return hashlib.md5(content.encode()).hexdigest()


@firestore.transactional
def _try_start_processing_transaction(
    transaction,
    doc_ref: firestore.DocumentReference,
    bucket_name: str,
    file_name: str,
    owner: str,
) -> bool:
    """トランザクション内でアトミックに処理開始をマークし、重複実行を防ぐ。

    ドキュメントが既に存在すれば（処理中/処理済み）False を返す。
    """
    doc = doc_ref.get(transaction=transaction)

    now = datetime.now(UTC)
    if doc.exists and not _check_existing_claim(doc.to_dict(), now):
        return False

    doc_data = {
        'bucket_name': bucket_name,
        'file_name': file_name,
        'started_at': now,
        'status': 'processing',
        'owner': owner,
        'expire_at': now + timedelta(days=10),
    }
    transaction.set(doc_ref, doc_data)
    print(f"処理開始をマーク: {doc_ref.id}")
    return True


def try_start_processing(event_id: str, bucket_name: str, file_name: str, owner: str) -> bool:
    """イベント処理の開始を試行する。重複実行なら False。"""
    db = _get_db()
    doc_ref = db.collection(config.collection_name()).document(event_id)
    transaction = db.transaction()
    return _try_start_processing_transaction(transaction, doc_ref, bucket_name, file_name, owner)


@firestore.transactional
def _finish_transaction(transaction, doc_ref, owner: str, completed: bool) -> None:
    snapshot = doc_ref.get(transaction=transaction)
    if not snapshot.exists:
        raise ClaimLost("処理記録がありません")
    data = snapshot.to_dict()
    _check_owner(data, owner)
    if data.get("status") != "processing":
        raise ClaimLost("処理は既に終了しています")
    if completed:
        now = datetime.now(UTC)
        transaction.update(doc_ref, {
            'completed_at': now,
            'status': 'completed',
            'expire_at': now + timedelta(days=10),
        })
    else:
        transaction.delete(doc_ref)


def mark_processing_completed(event_id: str, bucket_name: str, file_name: str, owner: str) -> bool:
    """所有権を確認して完了を記録する。"""
    db = _get_db()
    doc_ref = db.collection(config.collection_name()).document(event_id)
    _finish_transaction(db.transaction(), doc_ref, owner, True)
    return True


def mark_processing_failed(event_id: str, bucket_name: str, file_name: str, owner: str) -> bool:
    """所有する記録だけを削除して再試行を可能にする。"""
    db = _get_db()
    doc_ref = db.collection(config.collection_name()).document(event_id)
    _finish_transaction(db.transaction(), doc_ref, owner, False)
    return True

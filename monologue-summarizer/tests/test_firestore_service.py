from datetime import UTC, datetime, timedelta

import firestore_service
from firestore_service import generate_event_id, mark_processing_failed


class _FakeDocRef:
    def __init__(self):
        self.deleted = False
        self.updated = None

    def delete(self):
        self.deleted = True

    def update(self, data):
        self.updated = data


class _FakeCollection:
    def __init__(self, doc_ref):
        self._doc_ref = doc_ref

    def document(self, event_id):
        return self._doc_ref


class _FakeDb:
    def __init__(self, doc_ref):
        self._doc_ref = doc_ref

    def collection(self, name):
        return _FakeCollection(self._doc_ref)

    def transaction(self):
        return None


class TestGenerateEventId:
    def test_is_deterministic_for_same_inputs(self):
        a = generate_event_id("bucket", "file.m4a")
        b = generate_event_id("bucket", "file.m4a")
        assert a == b


class TestMarkProcessingFailed:
    def test_deletes_document_to_allow_retry(self, monkeypatch):
        doc_ref = _FakeDocRef()
        monkeypatch.setattr(firestore_service, "_get_db", lambda: _FakeDb(doc_ref))

        monkeypatch.setattr(firestore_service, "_finish_transaction", lambda t, d, o, c: d.delete())
        ok = mark_processing_failed("eid", "bucket", "file.m4a", "owner")

        assert ok is True
        assert doc_ref.deleted is True

    def test_returns_false_when_delete_raises(self, monkeypatch):
        doc_ref = _FakeDocRef()
        doc_ref.delete = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        monkeypatch.setattr(firestore_service, "_get_db", lambda: _FakeDb(doc_ref))

        import pytest

        monkeypatch.setattr(firestore_service, "_finish_transaction", lambda t, d, o, c: d.delete())
        with pytest.raises(RuntimeError, match="x"):
            mark_processing_failed("eid", "bucket", "file.m4a", "owner")


class TestMarkProcessingCompleted:
    def test_sets_timezone_aware_expire_at(self, monkeypatch):
        class Snapshot:
            exists = True

            def to_dict(self):
                return {"owner": "owner", "status": "processing"}

        class Document:
            def get(self, transaction):
                return Snapshot()

        class Transaction:
            updated = None

            def update(self, doc, data):
                self.updated = data

        transaction = Transaction()
        firestore_service._finish_transaction.to_wrap(transaction, Document(), "owner", True)
        assert transaction.updated["status"] == "completed"
        expire_at = transaction.updated["expire_at"]
        assert isinstance(expire_at, datetime)
        # datetime.now(UTC) を使うので tz-aware であること。
        assert expire_at.tzinfo is not None
        completed_at = transaction.updated["completed_at"]
        assert (expire_at - completed_at).days == 10
        # utcnow() の naive datetime ではないことを念のため確認。
        assert completed_at.tzinfo == UTC


def test_処理開始時のFirestore障害は重複扱いせず伝える(monkeypatch):
    import pytest

    def fail():
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(firestore_service, "_get_db", fail)
    with pytest.raises(RuntimeError, match="Firestore unavailable"):
        firestore_service.try_start_processing("eid", "bucket", "file.m4a", "owner")


def test_期限内の処理中は再配信が必要():

    import pytest

    now = datetime.now(UTC)
    with pytest.raises(firestore_service.ProcessingInProgress):
        firestore_service._check_existing_claim(
            {"status": "processing", "started_at": now - timedelta(minutes=1)}, now
        )


def test_期限切れの処理は引き継げる():

    now = datetime.now(UTC)
    assert (
        firestore_service._check_existing_claim(
            {"status": "processing", "started_at": now - timedelta(minutes=11)}, now
        )
        is True
    )


def test_所有権を失った処理は完了記録を更新できない():
    import pytest

    with pytest.raises(firestore_service.ClaimLost):
        firestore_service._check_owner({"owner": "new"}, "old")


def test_完了済み記録は失敗処理で消さない():
    import pytest

    class Snapshot:
        exists = True

        def to_dict(self):
            return {"owner": "owner", "status": "completed"}

    class Document:
        def get(self, transaction):
            return Snapshot()

    class Transaction:
        def delete(self, doc):
            pytest.fail("完了済み記録が削除された")

    with pytest.raises(firestore_service.ClaimLost):
        firestore_service._finish_transaction.to_wrap(Transaction(), Document(), "owner", False)


def test_引き継がれた処理は完了も失敗も記録を変更できない():
    import pytest

    class Snapshot:
        exists = True

        def to_dict(self):
            return {"owner": "new", "status": "processing"}

    class Document:
        def get(self, transaction):
            return Snapshot()

    class Transaction:
        def update(self, *args):
            pytest.fail("新しい所有者の記録が更新された")

        def delete(self, *args):
            pytest.fail("新しい所有者の記録が削除された")

    for completed in (True, False):
        with pytest.raises(firestore_service.ClaimLost):
            firestore_service._finish_transaction.to_wrap(
                Transaction(), Document(), "old", completed
            )


def test_期限切れの記録を新しい所有者が引き継ぐ():
    class Snapshot:
        exists = True

        def to_dict(self):
            return {"status": "processing", "owner": "old",
                    "started_at": datetime.now(UTC) - timedelta(minutes=11)}

    class Document:
        id = "event"

        def get(self, transaction):
            return Snapshot()

    class Transaction:
        def set(self, doc, data):
            self.data = data

    transaction = Transaction()
    assert firestore_service._try_start_processing_transaction.to_wrap(
        transaction, Document(), "bucket", "audio", "new"
    )
    assert transaction.data["owner"] == "new"
    assert transaction.data["status"] == "processing"

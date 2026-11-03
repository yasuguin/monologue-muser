import cloud_storage_service


def test_イベントの世代だけを読み取り削除する(monkeypatch):
    calls = []

    class Blob:
        def open(self, mode, **kwargs):
            calls.append(("open", mode, kwargs))
            return "reader"

        def delete(self, **kwargs):
            calls.append(("delete", kwargs))

    class Bucket:
        def blob(self, name, **kwargs):
            calls.append(("blob", name, kwargs))
            return Blob()

    class Client:
        def bucket(self, name):
            return Bucket()

    monkeypatch.setattr(cloud_storage_service, "_get_client", Client)
    assert cloud_storage_service.open_file_from_gcs("b", "a", 42) == "reader"
    assert cloud_storage_service.delete_file_from_gcs("b", "a", 42)
    assert calls == [
        ("blob", "a", {"generation": 42}),
        ("open", "rb", {"chunk_size": 8 * 1024 * 1024}),
        ("blob", "a", {}),
        ("delete", {"if_generation_match": 42}),
    ]

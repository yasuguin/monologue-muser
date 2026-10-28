import cloud_storage_service


def test_イベントの世代だけを読み取り削除する(monkeypatch):
    calls = []

    class Blob:
        def download_to_filename(self, path):
            calls.append(("download", path))

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
    assert cloud_storage_service.download_file_from_gcs("b", "a", "/tmp/a", 42)
    assert cloud_storage_service.delete_file_from_gcs("b", "a", 42)
    assert calls == [
        ("blob", "a", {"generation": 42}),
        ("download", "/tmp/a"),
        ("blob", "a", {}),
        ("delete", {"if_generation_match": 42}),
    ]

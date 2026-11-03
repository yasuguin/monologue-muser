from google.cloud import storage

# storage.Client は import 時ではなく初回利用時に生成する。
# 認証なしのテスト環境で import できること、Cloud Run コールドスタートでの遅延初期化が目的。
_storage_client: storage.Client | None = None


def _get_client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


# google-genai の 1 回の読み取り（8MB）ごとに GCS から 1 範囲だけ取り寄せる。
_READ_CHUNK_SIZE = 8 * 1024 * 1024


def open_file_from_gcs(bucket_name, file_name, generation):
    """イベントの世代の GCS オブジェクトを、読んだ分だけ取り寄せるファイルとして開く。

    Cloud Run の /tmp はメモリ上にあり、数百 MB の音声を落とすとメモリ上限を超えるため、
    ローカルに保存せず Gemini へそのまま流す。
    """
    blob = _get_client().bucket(bucket_name).blob(file_name, generation=generation)
    return blob.open("rb", chunk_size=_READ_CHUNK_SIZE)


def delete_file_from_gcs(bucket_name, file_name, generation):
    """GCS からファイルを削除する。成功で True。"""
    try:
        bucket = _get_client().bucket(bucket_name)
        blob = bucket.blob(file_name)
        blob.delete(if_generation_match=generation)
        print(f"GCSバケット '{bucket_name}' からファイル '{file_name}' を削除しました。")
        return True
    except Exception as e:
        print(f"GCSからのファイル削除中にエラーが発生しました: {e}")
        return False

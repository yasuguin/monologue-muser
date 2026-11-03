import uuid

import functions_framework
from cloud_storage_service import delete_file_from_gcs, open_file_from_gcs
from config import require_all
from firestore_service import (
    generate_event_id,
    mark_processing_completed,
    mark_processing_failed,
    try_start_processing,
)
from gemini_service import transcribe_and_summarize
from notion_service import send_to_notion


@functions_framework.cloud_event
def monologue_summarizer(cloud_event):
    """GCS へのファイルアップロードをトリガーに音声ファイルを処理する関数。"""
    # 必須 env の欠落は例外で止め、Cloud Run のエラーログに出す。
    require_all()

    data = cloud_event.data
    bucket_name = data["bucket"]
    file_name = data["name"]
    generation = int(data["generation"])

    # 重複実行防止: Firestore トランザクションでアトミックに処理開始をマークする。
    event_id = generate_event_id(bucket_name, file_name, str(generation))
    print(f"生成されたイベントID: {event_id} (バケット: {bucket_name}, ファイル: {file_name})")

    owner = uuid.uuid4().hex
    if not try_start_processing(event_id, bucket_name, file_name, owner):
        print(f"イベント {event_id} は既に処理済みまたは処理中です処理をスキップします")
        return "Event already processed or in progress"

    print(f"新規処理開始: ファイル名={file_name}, バケット名={bucket_name}, イベントID={event_id}")

    try:
        # 1. GCS の音声を読みながら Gemini API へ送り、文字起こしと要約を実行
        with open_file_from_gcs(bucket_name, file_name, generation) as audio:
            # MIME タイプはアップロード時に拡張子から決めた Content-Type を使う。
            result_json = transcribe_and_summarize(
                audio, data["contentType"], f"gs://{bucket_name}/{file_name}"
            )
        markdown_content = result_json["markdown"]
        next_action_list = result_json["nextActions"]
        next_action_markdown = (
            "### NextActions\n" + "\n".join(f"- {action}" for action in next_action_list)
            if next_action_list
            else ""
        )
        tags = result_json["tags"]

        # 2. Notion に結果を送信
        if not send_to_notion(file_name, markdown_content, next_action_markdown, tags):
            raise RuntimeError("Notionへの送信中にエラーが発生しました")

        # 完了記録が失敗した場合も音声を残し、再配信でやり直せるようにする。
        if not mark_processing_completed(event_id, bucket_name, file_name, owner):
            raise RuntimeError("処理完了記録に失敗しました")

    except Exception as e:
        print(f"音声処理中にエラーが発生しました: {e}")
        # 例外時も重複防止ドキュメントを消して再試行可能にする。
        mark_processing_failed(event_id, bucket_name, file_name, owner)
        raise

    # 完了後の削除失敗では完了記録を消さない。
    delete_file_from_gcs(bucket_name, file_name, generation)
    return "処理が正常に完了しました"

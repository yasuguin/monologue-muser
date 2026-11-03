import json
import textwrap
from collections.abc import Callable
from typing import BinaryIO

import config
import google.auth.exceptions
import httpx
from google import genai
from google.genai import errors, types
from schema import SummaryResponse, TranscriptionResponse

# genai.Client は import 時ではなく初回利用時に生成する。
# 認証なしのテスト環境で import できること、Cloud Run コールドスタートでの遅延初期化が目的。
_genai_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=config.gemini_api_key())
    return _genai_client


# 無料枠（Gemini Developer API の API キー）を使い切ったときだけ、実行時の SA で
# Agent Platform（Vertex AI）を従量で呼ぶ。global は gemini-2.5-flash を受けるエンドポイント。
_VERTEX_LOCATION = "global"
_vertex_client: genai.Client | None = None


def _get_vertex_client() -> genai.Client:
    global _vertex_client
    if _vertex_client is None:
        _vertex_client = genai.Client(
            vertexai=True, project=config.project_id(), location=_VERTEX_LOCATION
        )
    return _vertex_client


# 無料枠の上限を一度踏んだら、この instance の間は Agent Platform を先に呼ぶ。
# 上限は分・日単位で戻るので、次の instance からはまた無料枠を先に試す。
_prefer_agent_platform = False

_QUOTA_WORDS = ("quota", "resource_exhausted", "too many requests", "rate limit", "exceeded")


class UnparsableResponseError(RuntimeError):
    """structured output を解釈できない応答。別の経路なら解釈できる応答が返り得る。"""


def _is_quota_exhausted(error: Exception) -> bool:
    if not isinstance(error, errors.APIError):
        return False
    text = f"{error.status} {error.message}".lower()
    return error.code == 429 or any(word in text for word in _QUOTA_WORDS)


def _can_fall_back(error: Exception) -> bool:
    """経路を変えれば直り得る失敗か。400・404 のような依頼そのものの誤りは別の経路でも直らない。"""
    if isinstance(error, errors.APIError):
        return error.code in (403, 429) or error.code >= 500 or _is_quota_exhausted(error)
    return isinstance(
        error, (httpx.TransportError, google.auth.exceptions.GoogleAuthError,
                UnparsableResponseError)
    )


def _call_with_fallback[T](task: str, developer_api: Callable[[], T],
                           agent_platform: Callable[[], T]) -> T:
    """無料枠（Developer API）と Agent Platform を順に試す。

    上限を踏んだ後は Agent Platform を先にする。
    """
    global _prefer_agent_platform
    routes = [("Developer API", developer_api), ("Agent Platform", agent_platform)]
    if _prefer_agent_platform:
        routes.reverse()
    for index, (name, call) in enumerate(routes):
        try:
            return call()
        except Exception as e:
            if index == len(routes) - 1 or not _can_fall_back(e):
                raise
            if name == "Developer API" and _is_quota_exhausted(e):
                _prefer_agent_platform = True
            print(f"{task}を {name} で呼べなかったため {routes[index + 1][0]} でやり直します: {e}")
    raise AssertionError("経路が空")


# プロンプト文字列は仕様の一部。文言は変更しない。
_TRANSCRIPTION_PROMPT = textwrap.dedent("""
    <goal>ボイスメモの内容を正確に文字に起こしてください。</goal>
    <role>あなたは音声文字起こしの専門家で、話し言葉を正確にテキストに変換することが得意です。</role>
    <premise>この音声は独り言で考え事をしているボイスメモです。話題が突然変わったり、元の話題に戻ったりします。何か正解を出したいというよりは無数に浮かぶアイディアを言い連ねています。</premise>
    <goal-detail>
        <1>音声内容をできるだけ正確に文字に起こしてください。</1>
        <2>「ええと」や「あーー」などの言い淀みは除去して構いません。</2>
        <3>話し手の感情や語調のニュアンスをできるだけ残すようにしてください。</3>
        <4>文脈を無くさないように、話し言葉の自然な流れを維持してください。</4>
    </goal-detail>
    <output-format>
    純粋な文字起こしのテキストを出力してください。
    ```json
    {
        "transcription": "文字起こしの内容をここに記載"
    }
    ```
    </output-format>
    <instructions>
        <step1>ボイスメモを日本語で純粋に文字起こししてください。</step1>
        <step2>「ええと」や「あーー」など言葉に詰まっている部分を削除して、聞きやすい文章にしてください。</step2>
        <step3>話の流れや文脈、感情表現はそのまま保持してください。</step3>
        <step4>整形や要約はせず、元の発言内容をそのまま文字に起こしてください。</step4>
    </instructions>
""")


def _build_summary_prompt(transcription: str) -> str:
    # プロンプト文字列は仕様の一部。文言は変更しない。
    return textwrap.dedent(f"""
        <goal>文字起こしされたボイスメモの内容を整理して、きれいなメモにまとめてください。</goal>
        <role>あなたはソクラテスの弟子なみの聞き上手で、独り言のメモから情報が整理されたメモを作成することが得意です。</role>
        <premise>この文字起こしは独り言で考え事をしているボイスメモです。話題が突然変わったり、元の話題に戻ったりします。何か正解を出したいというよりは無数に浮かぶアイディアを言い連ねています。</premise>
        <goal-detail>
            <1>話題ごとに見出し（###）を作って、情報の順番を整理してください。話している順番に関わらず、設定した見出しに内容を足していってください。</1>
            <2>感情や心情を省略、要約せずに、できるだけニュアンスを残すようにしてください。</2>
            <3>箇条書きのようにきれいにまとめないでください。話の文脈を無くさないように、話し言葉口調のままの文章でまとめてください。</3>
            <4>整理はしますが、情報の要約はしないでください。内容の本質は保持してください。</4>
            <5>全体を見て「〇〇する！」「〇〇してみようかな」「〇〇興味ある」みたいに次の行動をするべきものを next-actionsにまとめてください。</5>
        </goal-detail>
        <output-format>
        Notionに適したMarkdown形式で出力してください。
        見出しは細かく作成することを意識し、1つの見出しに対して、最大でも150文字以内くらいにまとめてください。
        ```json
        {{
            "markdown": "### 話題1\\n内容1\\n内容2\\n### 話題2\\n内容1\\n内容2\\n### 話題3\\n内容1\\n内容2\\n...(いくつでも)",
            "nextActions": ["やること1", "やること2", "やること3"],
            "tags": ["タグ1", "タグ2", "タグ3"]
        }}
        ```
        </output-format>
        <instructions>
            <step1>このボイスメモを日本語で純粋に文字起こししてください。</step1>
            <step2>「ええと」や「あーー」など言葉に詰まっている部分などを削除して、きれいな文章にしてください。</step2>
            <step3>文章の中から話題（トピックス）を任意の数選定して、題名（タイトル）をつけ、マークダウンで見出し（###）を作ってください。タイトルはその話題の中での気づきや、結論などを短い文章にしたものを設定。イメージはインタビュー記事の段落ごとについているタイトルのような感じ。</step3>
            <step4>考え事の内容をできるだけ削らないようにして、各見出しの中にまとめてください。</step4>
            <step5>step4までの内容をまとめて、1つのMarkdownを出力してください。</step5>
            <step6>整理した内容の話題の中から「タグ」を最大3つ重要な順に生成してください。何についての話題なのか簡単に特定できるように具体的かつ短い単語にしてください。（例：プログラミング、英語、恋愛、AI、転職...）具体的な内容が分かりづらいものはやめて。</step6>
        </instructions>
        <transcription>
            {transcription}
        </transcription>
    """)  # noqa: E501  プロンプト本文は仕様であり改行で折り返せない


def _generate_transcription(client: genai.Client, audio) -> str:
    transcription_response = client.models.generate_content(
        model=config.gemini_model(),
        contents=[_TRANSCRIPTION_PROMPT, audio],
        config={
            'response_mime_type': 'application/json',
            'response_schema': TranscriptionResponse,
        },
    )
    parsed: TranscriptionResponse | None = transcription_response.parsed
    if parsed is not None:
        # structured output が取れたときはそれを使う。
        return json.loads(parsed.model_dump_json())['transcription']
    # parsed が None の場合は素のテキストへフォールバック（schema 適合に失敗した応答）。
    return transcription_response.text


def _transcribe(file: BinaryIO, mime_type: str, gcs_uri: str) -> str:
    """音声ファイルを文字起こししてテキストを返す。"""

    def developer_api() -> str:
        client = _get_client()
        # ファイルオブジェクトからは SDK が MIME タイプを推定できない。
        audio_file = client.files.upload(file=file, config={"mime_type": mime_type})
        return _generate_transcription(client, audio_file)

    def agent_platform() -> str:
        # Agent Platform では Files API が使えないため、GCS の音声を URI のまま渡す。
        audio_part = types.Part.from_uri(file_uri=gcs_uri, mime_type=mime_type)
        return _generate_transcription(_get_vertex_client(), audio_part)

    return _call_with_fallback("文字起こし", developer_api, agent_platform)


def _generate_summary(client: genai.Client, transcription: str) -> dict:
    summary_response = client.models.generate_content(
        model=config.gemini_model(),
        contents=_build_summary_prompt(transcription),
        config={
            'response_mime_type': 'application/json',
            'response_schema': SummaryResponse,
        },
    )
    parsed: SummaryResponse | None = summary_response.parsed
    if parsed is None:
        raise UnparsableResponseError("要約の応答を SummaryResponse として解釈できませんでした")
    return json.loads(parsed.model_dump_json())


def _summarize(transcription: str) -> dict:
    """文字起こしテキストを整形・要約し、SummaryResponse 相当の dict を返す。"""
    return _call_with_fallback(
        "要約",
        lambda: _generate_summary(_get_client(), transcription),
        lambda: _generate_summary(_get_vertex_client(), transcription),
    )


def transcribe_and_summarize(file: BinaryIO, mime_type: str, gcs_uri: str) -> dict:
    """音声ファイルを文字起こし→整形要約し、markdown / nextActions / tags を返す。

    gcs_uri は file と同じ音声の GCS 上の場所で、無料枠を使い切ったときに使う。

    失敗は呼び出し元に伝え、イベントの再配信につなげる。
    """
    try:
        transcription = _transcribe(file, mime_type, gcs_uri)
    except Exception as e:
        print(f"文字起こし中にエラーが発生しました: {e}")
        raise

    try:
        return _summarize(transcription)
    except Exception as e:
        print(f"文字起こしまたは要約中にエラーが発生しました: {e}")
        raise

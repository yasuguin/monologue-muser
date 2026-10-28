from pydantic import BaseModel, Field


class TranscriptionResponse(BaseModel):
    transcription: str = Field(description="ボイスメモの正確な文字起こし")


class SummaryResponse(BaseModel):
    markdown: str = Field(description="Notionに最適化されたMarkdown形式のまとめノート")
    nextActions: list[str] = Field(
        description="ボイスメモの内容から次にするべき行動をリスト化(0〜3個)"
    )
    # tags の説明は Gemini への指示の一部なので文言を変えない（プロンプト同等の仕様）。
    tags: list[str] = Field(
        description=(
            "notion_markdownの内容のトピックス・話題を見て、「タグ」を生成。"
            "何についての話題なのか簡単に特定できるように具体的かつ短い単語にしてください。"
            "（例：プログラミング、恋愛、AI、転職...）"
        )
    )

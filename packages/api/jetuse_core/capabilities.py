"""能力ディスクリプタ登録簿(specs/17 §3-4 / SP1-01)。ビルダー(SP3)が読む能力カタログの正本。

能力の追加手順(これが正本):
1. ルートを service/routes/ に生やす(通常のルート追加)。
2. 本ファイルの CAPABILITIES に descriptor を1件追記する(routes には実在の path/method を書く)。
3. 以上で GET /api/capabilities に自動で載る(routes の乖離は tests/test_capabilities.py が検出)。
"""

# ponytail: 素の dict のリスト。統一 Capability インターフェース(案2)は作らない(specs/17 §3)。
CAPABILITIES: list[dict] = [
    {
        "capability": "chat",
        "summary": "LLM と対話する(SSE ストリーミング。モデル選択・システムプロンプト・画像入力可)",
        "when_to_use": "汎用の対話 UI。アシスタント・質問応答・文章生成などデモの基本形。",
        "example": {
            "input": {"model": "openai.gpt-oss-120b",
                      "messages": [{"role": "user", "content": "OCIの利点を3つ教えて"}]},
            "output": "SSE で data フレームにトークンが逐次届き、data: [DONE] で終端。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/chat/stream", "method": "post"},
            {"path": "/api/chat/models", "method": "get"},
            {"path": "/api/demos/{demo_id}/chat", "method": "post"},  # デモスコープ(SP1-03)
        ],
    },
    {
        "capability": "rag.search",
        "summary": "アップロードした文書への検索 Q&A(引用付き回答)",
        "when_to_use": "社内文書・マニュアル・規程集など「手元の文書に基づいて答える」デモ。",
        "example": {
            "input": {"model": "openai.gpt-oss-120b", "rag": True,
                      "messages": [{"role": "user", "content": "経費精算の締め日は?"}]},
            "output": "文書由来の回答が SSE で届き、末尾に citations(引用元ファイル名)が付く。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/chat/stream", "method": "post"},
            {"path": "/api/rag/files", "method": "get"},
            {"path": "/api/rag/files", "method": "post"},
            # デモスコープ(SP1-03)
            {"path": "/api/demos/{demo_id}/chat", "method": "post"},
            {"path": "/api/demos/{demo_id}/rag/files", "method": "get"},
            {"path": "/api/demos/{demo_id}/rag/files", "method": "post"},
        ],
    },
    {
        "capability": "dbchat",
        "summary": "自然言語からSQLを生成しデータベースを照会する(NL2SQL + 実行 + グラフ化)",
        "when_to_use": "売上分析・在庫照会など「データベースに日本語で質問する」デモ。",
        "example": {
            "input": {"question": "月別の売上合計を教えて"},
            "output": "生成された SELECT 文が SSE で届く。"
                      "/api/dbchat/execute で実行し行データを得る。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/chat/nl2sql", "method": "post"},
            {"path": "/api/dbchat/execute", "method": "post"},
            {"path": "/api/dbchat/schema", "method": "get"},
        ],
    },
    {
        "capability": "agents",
        "summary": "ツール(Web検索・RAG検索・DB照会・MCP)を自律的に使うエージェントを実行する",
        "when_to_use": "複数ステップの調査・ツール連携を見せるデモ。"
                       "定義済みエージェントを選んで対話させる。",
        "example": {
            "input": {"model": "openai.gpt-oss-120b", "agent_id": "<GET /api/agents のid>",
                      "messages": [
                          {"role": "user", "content": "最新のOCIリリースを調べて要約して"}]},
            "output": "ツール呼び出しの経過と最終回答が SSE で届く。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/agents", "method": "get"},
            {"path": "/api/chat/stream", "method": "post"},
            {"path": "/api/agent/execute-tool", "method": "post"},
        ],
    },
    {
        "capability": "voice",
        "summary": "音声の文字起こし(リアルタイムSTT)と音声合成(TTS)",
        "when_to_use": "音声入力・読み上げを含むデモ(窓口応対・音声メモなど)。",
        "example": {
            "input": {"note": "POST /api/stt/sessions でセッション作成→audio へ音声チャンクを送信"},
            "output": "events(SSE)で部分/確定の文字起こしが届く。"
                      "/api/tts はテキストから音声(audio/mp3)を返す。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/stt/sessions", "method": "post"},
            {"path": "/api/stt/sessions/{sid}/audio", "method": "post"},
            {"path": "/api/stt/sessions/{sid}/events", "method": "get"},
            {"path": "/api/tts", "method": "post"},
        ],
    },
    {
        "capability": "minutes",
        "summary": "会議音声から議事録を作る(文字起こし + 要約・アクション抽出)",
        "when_to_use": "会議・打合せの録音から議事録を自動生成するデモ。",
        "example": {
            "input": {"note": "POST /api/minutes で音声ファイルを登録→ /generate で議事録生成"},
            "output": "文字起こし全文と、要約・決定事項・TODO を構造化した議事録が得られる。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/minutes", "method": "post"},
            {"path": "/api/minutes/{mid}", "method": "get"},
            {"path": "/api/minutes/{mid}/generate", "method": "post"},
        ],
    },
    {
        "capability": "translate",
        "summary": "テキスト翻訳(LLM / OCI Language の2バックエンド)",
        "when_to_use": "多言語対応・翻訳支援のデモ。対応言語は /api/translate/options で取得。",
        "example": {
            "input": {"text": "こんにちは", "target": "en"},
            "output": {"translated": "Hello"},
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/translate", "method": "post"},
            {"path": "/api/translate/options", "method": "get"},
        ],
    },
    {
        "capability": "docunderstand",
        "summary": "文書画像/PDFからのテキスト・表・キー値抽出(OCR / Document Understanding / VLM)",
        "when_to_use": "帳票・請求書・スキャン文書の読み取りデモ。ファイルを multipart で送る。",
        "example": {
            "input": {"note": "multipart/form-data で file(png/jpg/pdf)を送信。"
                              "engine/language 指定可"},
            "output": "抽出テキスト・表・キー値ペアの JSON が返る。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/ocr", "method": "post"},
            {"path": "/api/ocr/options", "method": "get"},
        ],
    },
]

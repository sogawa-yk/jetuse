"""能力ディスクリプタ登録簿(specs/17 §3-4 / SP1-01)。ビルダー(SP3)が読む能力カタログの正本。

能力の追加手順(これが正本):
1. ルートを service/routes/ に生やす(通常のルート追加)。
2. 本ファイルの CAPABILITIES に descriptor を1件追記する(routes には実在の path/method を書く)。
3. 以上で GET /api/capabilities に自動で載る(routes の乖離は tests/test_capabilities.py が検出)。
"""

from . import http_tools

# --- RAG バックエンドの能力差(RAGM-03 / ADR-0020 §3) ---------------------------
# 「Oracle AI Database を選ぶと何が増えるか」を機械可読で示す。軸は比較ドキュメント
# docs/comparison/rag-metadata-backends.md と揃える(ここを増やすときは向こうも直す)。
#
# **未実証のものを「できる」と書かない**のがこの表の存在理由である。実機で確かめていない
# 項目は support="unverified"(= verified False)にする。設計上できるはずでも、実行結果が
# 無いものは未実証(例: VPD による行レベル制御・業務表との JOIN は SPIKE-M1 の非ゴールで
# 実行結果が無い = SKIPPED.md 1/3)。この不変条件は _axis() が構造で守り、
# tests/test_capabilities.py が検査する。
RAG_BACKEND_AXES: tuple[str, ...] = (
    "citation_granularity",        # 出典粒度
    "filter_expressiveness",       # 絞り込みの表現力
    "business_data_join",          # 業務データ結合
    "row_level_security",          # 行レベル制御
    "metadata_update_consistency",  # メタ更新の整合性
)
# yes=できる / limited=条件付きでできる / no=できない / unverified=未実証(できるとは書かない)
RAG_SUPPORT_LEVELS: tuple[str, ...] = ("yes", "limited", "no", "unverified")

_COMPARISON = "docs/comparison/rag-metadata-backends.md"


def _axis(support: str, detail: str, evidence: str) -> dict:
    """1 軸の記述。verified は support から導出する(手で立てさせない)。

    verified=True は「この記述に一次情報(実測 / 実機で確認した制約)の裏付けがある」であり、
    yes/limited なら能力の実証、no なら実現できないことの根拠が evidence にある、という意味。
    verified=False は unverified のときだけ = 確かめていない = できるとは言えない。
    """
    if support not in RAG_SUPPORT_LEVELS:
        raise ValueError(f"unknown support level: {support}")
    return {"support": support, "verified": support != "unverified",
            "detail": detail, "evidence": evidence}


RAG_BACKEND_CAPABILITIES: dict = {
    "axes": RAG_BACKEND_AXES,
    "support_levels": {
        "yes": "実機で確認済み。使える",
        "limited": "使えるが条件・制約がある",
        "no": "この方式では実現できない",
        "unverified": "未実証。できるとは言えない(顧客に言う前に実機確認が要る)",
    },
    "comparison_doc": _COMPARISON,
    "backends": {
        "vector_store": {
            "label": "Enterprise AI マネージド Vector Store (file_search)",
            "role": "手軽さ側(既定)。追加開発ほぼ無しで文書 Q&A が出せる",
            "axes": {
                "citation_granularity": _axis(
                    "limited",
                    "ファイル単位。1 ファイルが複数チャンクに割れても属性は 1 種類しか返らない"
                    "(xlsx は sheet='(ブック全体: N シート)')。チャンク本文・スコアは返る。"
                    "チャンク単位の出典が要るなら 1 チャンク = 1 ファイルで取り込む運用が要る。",
                    "docs/verification/SPIKE-M1.md ①-a / docs/verification/PREP-01.md",
                ),
                "filter_expressiveness": _axis(
                    "limited",
                    "属性フィルタ eq/and/or/gte(in は不可・キー 16 / 値 512 文字 / 入れ子不可)。"
                    "版で絞るにはリクエストで rag_filters を明示指定する(既定は絞らないので"
                    "旧版が混ざりうる)。存在しないキーはエラーにならず 0 件になるため、"
                    "キーはアプリ側の許可制で守っている。",
                    "docs/verification/SPIKE-M1.md ①-b/①-d / docs/verification/RAGM-01.md",
                ),
                "business_data_join": _axis(
                    "no",
                    "別サービスのため、在庫表・契約表などの業務表と結合した検索はできない。",
                    _COMPARISON,
                ),
                "row_level_security": _axis(
                    "no",
                    "アクセス制御はストア分離と IAM。行(チャンク)単位の制御機構は無い。",
                    _COMPARISON,
                ),
                "metadata_update_consistency": _axis(
                    "limited",
                    "files.update(attributes=) で再取り込み無しに直せるが、API 越しの更新で"
                    "検索とは別トランザクション(結果整合)。",
                    "docs/verification/SPIKE-M1.md ①",
                ),
            },
            "notes": [
                "素の xlsx は 400 unsupported_file で拒否されるため、抽出テキストに変換して"
                "投入している(表示名・sha256 は原本のまま)。",
            ],
        },
        "adb": {
            "label": "Oracle AI Database 自前索引 (DBMS_VECTOR_CHAIN)",
            "role": "高機能側。業務データと絡む提案・監査が要る用途",
            "axes": {
                "citation_granularity": _axis(
                    "yes",
                    "チャンク単位。xlsx はシート名とセル範囲まで返る"
                    "(実測例: 『制約』C5:E6 / 『改訂履歴』A1:C2)。"
                    "同じファイル由来でもチャンクごとに違う出典になる。",
                    "docs/verification/PREP-01.md / docs/verification/RAGM-02.md",
                ),
                "filter_expressiveness": _axis(
                    # DB 側の表現力は SQL の WHERE そのものだが、**いま外から使える形**は
                    # 「常に現行版だけを検索する」までなので yes にしない(RAGM03-001)。
                    # 表の1行だけを見た人が「条件を渡して絞れる」と誤解する形は作らない。
                    "limited",
                    "検索は常に現行版のみ(current_version='Y')で、旧版が混ざらない。ただし"
                    "チャット API から条件を渡す口は無い(rag_filters は vector_store 専用で、"
                    "adb 指定時は 400)。DB 側は version / file / file_id / sheet / kind を"
                    "SQL の WHERE で絞れる実装があり、口を開けるかは別タスクの判断。",
                    "docs/verification/RAGM-02.md / rag_adb.FILTER_COLUMNS・generate()",
                ),
                "business_data_join": _axis(
                    "unverified",
                    "同じ DB の業務表と JOIN したベクタ検索が 1 SQL で書ける — が、"
                    "実行結果はまだ無い(SPIKE-M1 は業務表を作らず非ゴールにした)。"
                    "顧客に示す前に実機確認が要る。",
                    "runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md 3",
                ),
                "row_level_security": _axis(
                    "unverified",
                    "VPD / Data Redaction は同じ表に対する DB 機能なので原理的には併用できるが、"
                    "ベクタ検索に効くことは未実証(SPIKE-M1 の非ゴール)。"
                    "「行レベル制御ができる」と顧客に言う前に実機確認が要る。",
                    "runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md 3 / ADR-0020 未解決",
                ),
                "metadata_update_consistency": _axis(
                    "yes",
                    "UPDATE 1 文で直せて、検索・台帳と同一トランザクション"
                    "(現行版の付け替えも削除と同じトランザクションで整合する)。",
                    "docs/verification/RAGM-02.md",
                ),
            },
            "notes": [
                "ADB 23ai 以上・マイグレーション 017 適用が前提。"
                "未導入なら取り込み状況は disabled になる。",
                "埋め込みはクライアント側。本文は OCI Generative AI の embedText へ送られる"
                "(「テナント外に一切出ない」ではない)。",
            ],
        },
        "select_ai": {
            "label": "Select AI with RAG (DBMS_CLOUD_AI 索引)",
            "role": "バケットに置くだけで RAG。メタデータ管理の基盤としては採用しない",
            "axes": {
                "citation_granularity": _axis(
                    "limited",
                    "ファイル単位(object_name + start/end offset)。応答末尾に Sources: の"
                    "文字列として付き、スコアは無い。",
                    "docs/verification/SPIKE-M1.md ②",
                ),
                "filter_expressiveness": _axis(
                    "no",
                    "標準経路に絞り込みの口が無い。任意メタデータは ORA-20048 で拒否される。",
                    "docs/verification/SPIKE-M1.md ②",
                ),
                "business_data_join": _axis(
                    "no",
                    "$VECTAB は DB 内にあるが索引用途で、業務表との結合は実質できない。",
                    _COMPARISON,
                ),
                "row_level_security": _axis(
                    "unverified",
                    "DB 権限は効くが、行レベル制御がベクタ検索に効くことは未実証。",
                    "runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md 3",
                ),
                "metadata_update_consistency": _axis(
                    "no",
                    "索引リフレッシュで入った新規行は自前で足した列が NULL になる。"
                    "取り込みのたびに補完処理を回す前提の設計になる。",
                    "docs/verification/SPIKE-M1.md ②",
                ),
            },
            "notes": [
                "アップロードの反映は refresh_rate 次第(既定 60 分)。",
                "xlsx をこの経路でどう扱うかは実機確認中(PREP-02)で未確認。",
            ],
        },
        "opensearch": {
            "label": "OCI Search with OpenSearch",
            "role": "既存の選択肢。ADR-0020 のメタデータ比較の対象外",
            "axes": {
                axis: _axis(
                    "unverified",
                    "ADR-0020 / SPIKE-M1 の比較対象外で未計測。この軸について言えることは無い。",
                    _COMPARISON,
                )
                for axis in RAG_BACKEND_AXES
            },
            "notes": ["メタデータ・出典粒度の観点では未評価。評価するなら別タスクが要る。"],
        },
    },
}


# デモスコープルートの prefix(specs/19 §3.4 — プラン語彙の構造的導出に使う)
DEMO_SCOPE_PREFIX = "/api/demos/{demo_id}/"


def demo_plan_vocabulary(capabilities: list[dict] | None = None) -> list[str]:
    """プラン語彙 = demo_safe=true かつデモスコープルートを 1 つ以上持つ能力(specs/19 §3.4)。

    能力 id をハードコードせずカタログから導出する — デモスコープのパススルーを足せば
    語彙は自動で広がる(§3.4 案 A の追従性)。順序はカタログ順。
    """
    caps = CAPABILITIES if capabilities is None else capabilities
    return [
        c["capability"] for c in caps
        if c.get("demo_safe")
        and any(r["path"].startswith(DEMO_SCOPE_PREFIX) for r in c["routes"])
    ]


# ponytail: 素の dict のリスト。統一 Capability インターフェース(案2)は作らない(specs/17 §3)。
CAPABILITIES: list[dict] = [
    {
        "capability": "chat",
        "summary": "LLM と対話する(SSE ストリーミング。モデル選択・システムプロンプト・画像入力可)",
        "when_to_use": "汎用の対話 UI。アシスタント・質問応答・文章生成などデモの基本形。",
        "example": {
            "input": {"model": "gpt-oss-120b",
                      "messages": [{"role": "user", "content": "OCIの利点を3つ教えて"}]},
            "output": "SSE で data フレームにトークンが逐次届き、data: [DONE] で終端。",
        },
        "demo_safe": True,
        "routes": [
            {"path": "/api/chat/stream", "method": "post"},
            {"path": "/api/chat/models", "method": "get"},
            {"path": "/api/demos/{demo_id}/chat", "method": "post"},  # デモスコープ(SP1-03)
            # デモ会話の作成(SP2-03 / specs/18 §4.2 — 継続は chat に conversation_id)
            {"path": "/api/demos/{demo_id}/conversations", "method": "post"},
        ],
    },
    {
        "capability": "rag.search",
        "summary": "アップロードした文書への検索 Q&A(引用付き回答)",
        "when_to_use": "社内文書・マニュアル・規程集など「手元の文書に基づいて答える」デモ。",
        "example": {
            "input": {"model": "gpt-oss-120b", "rag": True,
                      "messages": [{"role": "user", "content": "経費精算の締め日は?"}]},
            "output": "文書由来の回答が SSE で届き、末尾に citations(引用元ファイル名)が付く。",
        },
        # RAGM-03: バックエンドを選べても「選ぶと何が増えるか」が分からなければ選べない。
        # 能力差は backend_capabilities に機械可読で載せる(ADR-0020 §3)。
        "backend_capabilities": RAG_BACKEND_CAPABILITIES,
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
            # デモスコープ(SP2-03 / specs/18 §4.3 — datasets ターゲット固定)
            {"path": "/api/demos/{demo_id}/dbchat/nl2sql", "method": "post"},
            {"path": "/api/demos/{demo_id}/dbchat/execute", "method": "post"},
            {"path": "/api/demos/{demo_id}/dbchat/schema", "method": "get"},
            {"path": "/api/demos/{demo_id}/db/datasets", "method": "get"},
            {"path": "/api/demos/{demo_id}/db/datasets", "method": "post"},
            {"path": "/api/demos/{demo_id}/db/datasets/generate", "method": "post"},
            {"path": "/api/demos/{demo_id}/db/datasets/{ds_id}/preview", "method": "get"},
            {"path": "/api/demos/{demo_id}/db/datasets/{ds_id}", "method": "delete"},
        ],
    },
    {
        "capability": "agents",
        "summary": "ツール(Web検索・RAG検索・DB照会・MCP・デモ側の外部HTTP API)を"
                   "自律的に使うエージェントを実行する",
        "when_to_use": "複数ステップの調査・ツール連携を見せるデモ。"
                       "定義済みエージェントを選んで対話させる。"
                       "デモ固有の業務APIを使わせたい場合は "
                       "/api/agent/http-tools に登録し、実行時に http_tool_ids で渡す。",
        "example": {
            "input": {"model": "gpt-oss-120b", "agent_id": "<GET /api/agents のid>",
                      "messages": [
                          {"role": "user", "content": "最新のOCIリリースを調べて要約して"}]},
            "output": "ツール呼び出しの経過と最終回答が SSE で届く。",
        },
        "demo_safe": True,
        # TOOL-01: 実測で確認できた範囲だけを書く(未実証を「できる」と書かない)
        "external_tools": {
            "how": "name/description/JSON Schema/URL/メソッドを /api/agent/http-tools に"
                   "登録し、POST /api/chat/stream に agent=true と http_tool_ids を渡す。"
                   "モデルが呼ぶと JetUse がサーバー側で HTTP を代理実行して結果を返す"
                   "(ブラウザからは叩かせない)。",
            "methods": ["GET", "POST"],
            "max_tools_per_agent": http_tools.MAX_TOOLS_PER_AGENT,
            "auth": "秘密は Vault に置き auth_secret_ocid で参照する。任意のヘッダ名"
                    "(既定 Authorization)に載せて送る。秘密は DB にも API 応答にも現れない。"
                    "使えるのは freeform タグ jetuse_tool_owner が登録者と一致し、"
                    "本アプリのコンパートメントにある秘密だけ(他人・運用用の秘密は登録できない)。",
            "url_policy": "https のみ。ループバック・内部レンジ・リンクローカル"
                          "(169.254.169.254 等)は登録時と実行時の両方で拒否する。",
            "limits": {
                "timeout_seconds": http_tools.TIMEOUT_SECONDS,
                "max_response_bytes": http_tools.MAX_RESPONSE_BYTES,
                "retries": 0,
                "redirects": "追わない(3xx は失敗)",
                "on_limit_exceeded": "黙って切り詰めず、ツール実行失敗としてモデルへ返す",
            },
        },
        "routes": [
            {"path": "/api/agents", "method": "get"},
            {"path": "/api/chat/stream", "method": "post"},
            {"path": "/api/agent/execute-tool", "method": "post"},
            {"path": "/api/agent/http-tools", "method": "get"},
            {"path": "/api/agent/http-tools", "method": "post"},
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

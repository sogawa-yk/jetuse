# リファクタリング後検証: 単体 / 結合 / パフォーマンス

- 日付: 2026-06-18
- ブランチ: `task/refactor-review-validation`(P0–P2 リファクタ commit/push 済み)
- 実行環境: OCI compute `dev`(ap-osaka-1)。**ライブ uvicorn**(`http://127.0.0.1:8000`,
  `AUTH_REQUIRED=false`)が実 OCI GenAI + dev ADB `jetusedev_low`(`JETUSE_APP`)に接続。
- 計測経路: **API Gateway を経由しない uvicorn 直結**。よって SPIKE-02 / CP2 の
  Gateway 経由値とは厳密な apples-to-apples ではなく、参考比較のみ。

---

## 1. 単体テスト(コード未変更・再実行)

| スイート | コマンド | 結果 |
|---|---|---|
| API | `packages/api $ ../../.venv/bin/pytest -q` | **135 passed**, 3 warnings, 8.90s |
| API カバレッジ | (同上, `--cov`) | **54.73%**(必須 45% 超過。`service/main.py` 100% / `service/schemas.py` 100% / `service/sse.py` 89% / `service/validators.py` 89%) |
| jetuse_shared | `.venv/bin/pytest packages/jetuse_shared/tests -q` | **28 passed**, 0.05s |
| web (vitest) | `packages/web $ npx vitest run` | **5 files / 48 tests passed**(dict 2, useChatStream 6, ucform 11, sse 18, buildChatRequest 11), 2.06s |

合計: バックエンド 163 passed、フロント 48 passed。全グリーン。

---

## 2. 結合テスト

### 2.1 TestClient golden(422 検証 + 9 router マウント)

`/tmp/integration_golden.py`(`from service.main import app` + `TestClient`)。
422 系は DB 不要。**20 checks 全 PASS**。
注: in-process TestClient はウォレット未設定のため DB 依存 GET は 503 を返す
(= router マウント確認としては非 404 で合格。実 DB 検証はライブ smoke で別途実施)。

| endpoint | case | status | expected |
|---|---|---|---|
| POST /api/agents | unknown model | 422 | 422 |
| POST /api/agents | code_interpreter ツール(コンテナ非対応) | 422 | 422 |
| POST /api/agents | mcp_server_ids 付与 | 422 | 422 |
| POST /api/agents | select_ai 非対応ツール | 422 | 422 |
| POST /api/usecases | フィールド名重複 | 422 | 422 |
| POST /api/usecases | select にオプションなし | 422 | 422 |
| POST /api/usecases | テンプレートがフィールド未参照 | 422 | 422 |
| POST /api/chat/stream | 未知モデル | 400 | 400 |
| POST /api/chat/stream | messages 空 | 422 | 422 |
| GET ×11(chat/agents/conversations/usecases/presets/translate・ocr opts/datasets/rag/admin/healthz) | router マウント | 非404 | 非404 |

### 2.2 ライブ smoke(httpx → `127.0.0.1:8000`, 実 OCI + ADB)

**3 つの agent-dispatch 経路:**

| 経路 | リクエスト | 結果 |
|---|---|---|
| **(a) ネイティブ ad-hoc チャット** | `chat/stream` llama-3.3-70b「2+2は?」 | **200**。delta 10件、TTFT 0.16s、total 0.32s。出力「2 + 2 は 4 です。」 |
| **(a2) ネイティブ ReAct ツール** | `chat/stream` gpt-oss-120b + `agent:true` + `enabled_tools:[get_current_time]` | **200**。**tool_call 1 / tool_result 1** イベント発火 → 最終回答に現在時刻。`stream_agent` 経路で ReAct 動作確認 |
| **(b) Select AI 経路** | `chat/nl2sql` backend=`select_ai`「チャネルの数は?」 | **200**。NL→SQL: `SELECT COUNT("CHANNEL_ID") AS "channel_count" FROM "SH"."CHANNELS"`(ADB `DBMS_CLOUD_AI`) |
| (b2) 比較: sql_search | 同上 backend=`sql_search` | **200**。`SELECT COUNT(*) AS CHANNEL_COUNT FROM SH.CHANNELS` |
| (b3) NL→結果クローズ | `dbchat/execute`(上記 SQL) | **200**。`CHANNEL_COUNT=5`(NL→SQL→実行の往復成立) |
| **(c) ホスト型 agent 経路** | agent 作成(`openai_agents`)→ `chat/stream` で実行 | **作成 200 → 実行 200 だが SSE 内に正直なエラーイベント**: `エージェント未設定: agent container not configured: sdk=openai_agents missing=['hosted_agent_idcs_domain','hosted_agent_client_id','hosted_agent_client_secret','hosted_agent_scope']`。本環境はホスト型コンテナ未デプロイのため**想定どおり**(HTTP は 200 のまま SSE error frame で返す設計) |

> 注: `dbchat/execute` は SQL 実行用エンドポイント(`{sql}` 必須)。NL 質問は `chat/nl2sql`
> が担当し、フロントが nl2sql → execute を連結する。(b)+(b3) で全工程を確認した。

**その他の機能(ライブ・正直記録):**

| 機能 | エンドポイント | 結果 |
|---|---|---|
| 翻訳(LLM) | POST /api/translate | **200**「こんにちは、世界」→「Hello, world」 |
| 翻訳オプション | GET /api/translate/options | **200**(7言語 + backends) |
| OCR(VLM) | POST /api/ocr engine=vlm | **200**。`JETUSE OCR 1234` 抽出(model gemini-2.5-pro) |
| OCR(Document Understanding) | POST /api/ocr engine=document | **200**。`JETUSE OCR 1234`、mean_confidence 0.969 |
| OCR オプション | GET /api/ocr/options | **200** |
| TTS(Phoenix) | POST /api/tts | **200**。音声バイト 5823B(インスタンス creds で動作) |
| 会話一覧 | GET /api/conversations | **200**(既存会話あり) |
| agents CRUD | POST→GET→DELETE→GET | **200/200/200(deleted:true)/404**。ADB 書込往復成立 |
| usecases | GET 一覧 / POST→DELETE | **200**(builtin 含む) / 作成・削除 200 |
| presets | GET 一覧 / POST→DELETE | **200**(空) / 作成・削除 200 |
| dbchat schema | GET /api/dbchat/schema | **200**(SH スキーマ・テーブル/カラム/コメント) |
| STT realtime セッション | POST→audio→DELETE | **200/200/200**(realtime はバケット不要。セッションは ADB に作成・破棄) |
| minutes 一覧 | GET /api/minutes | **200**(既存ジョブあり) |

作成したテストデータ(agent / usecase / preset / STT セッション)は DELETE で全て後始末済み。

---

## 3. パフォーマンス(ライブ・モデルコスト抑制のため小規模)

`POST /api/chat/stream`。TTFT = 最初の `delta` イベントまで、TOTAL = `[DONE]` まで。
プロンプト「日本の四季を1文ずつ…」、`max_tokens=256`。

| model | N | TTFT P50 / P95 / max (s) | TOTAL P50 / P95 / max (s) | 平均delta数 | 最大chunk間隔(s) |
|---|---|---|---|---|---|
| llama-3.3-70b | 5 | 0.15 / 0.15 / 0.15 | 1.73 / 3.87 / 3.87 | 87 | 0.72 |
| gpt-oss-120b | 5 | 0.35 / 0.58 / 0.58 | 0.84 / 0.99 / 0.99 | 107 | 0.08 |
| gemini-2.5-flash | 5 | 2.36 / 4.22 / 4.22 | 2.46 / 4.31 / 4.31 | 3 | 0.15 |
| gemini-2.5-pro | 2 | 3.85 / 3.92 / 3.92 | 3.91 / 3.94 / 3.94 | 2 | 0.08 |

所見:
- **TTFT**: gpt-oss / llama はサブ秒(token ストリーミング)。gemini 系は TTFT ≈ TOTAL
  で、まとまった少数 chunk(2–3 delta)で返るため初動が遅い(CP2 の傾向と一致)。
- **gemini-2.5-pro** は TTFT 約 3.9s。KEEPALIVE_SECONDS=15 設計(ADR-0003)で十分カバー範囲内。

### SSE バッファリング確認(`spikes/spike02_measure_sse.py` の考え方を踏襲)

- 全モデルで delta が逐次到着。**最大 inter-delta gap は 0.72s**(llama)で、
  どのモデルでも KEEPALIVE_SECONDS=15 を大きく下回る → **バッファリングなし(逐次配信)**。
- 各ストリーム冒頭に keepalive プリロールフレーム `{"ka":1}` を確認(`KEEPALIVE_FRAME`)。
  今回の短い生成では生成中アイドルが 15s に達しないため、生成中の追加 keepalive は発火せず
  (= 正常)。長時間 GENERATE(Select AI 初回索引・gemini-pro 長文)では 15s 間隔で発火する設計。
- 計測は uvicorn 直結。API Gateway 経由(SPIKE-02 / CP2-measurements.md, readTimeout 上限 300s)
  との比較は参考のみ。

---

## 4. 未設定サービス起因の想定挙動(正直な記録)

`get_settings()` 実測: `speech_bucket=''`, `hosted_agent_idcs_domain/client_id/scope=''`,
`opensearch_endpoint=''`(いずれも本環境で未構成)。

| 機能 | 挙動 | 種別 |
|---|---|---|
| ホスト型 agent 実行 | SSE error frame「agent container not configured … missing=[hosted_agent_*]」 | 想定どおり(コンテナ未デプロイ) |
| OpenSearch RAG | SSE error frame「OpenSearch RAGの実行に失敗しました: OpenSearch endpoint 未設定」 | 想定どおり |
| バッチ minutes 文字起こし(Speech バケット依存) | バケット未設定経路は到達せず一覧/既存ジョブのみ確認 | 想定どおり(音声 upload は未実施) |

> realtime STT セッション・TTS(Phoenix)・OCR(両エンジン)は **speech_bucket 不要**で
> インスタンス creds により動作した(503 にならない)。「Speech 系は全て 503」ではない点に注意。

---

## 5. 結論

- 単体(163 backend + 48 front)・結合(golden 20 + ライブ smoke 全経路)・パフォすべて取得。
- 3 つの agent-dispatch 経路((a)ネイティブ /(b)Select AI /(c)ホスト型)が分割後も
  正しくルーティングされることを実機で確認。(c) は未デプロイのため正直なエラーを返す。
- リファクタ(`main.py`/`chat.py` 分割、`jetuse_shared` 抽出、SSE 共通化)後も
  挙動・ステータス・SSE フレーム形状に退行なし。

成果物: 本レポート `/home/opc/jetuse/docs/verification/perf-refactor.md`。
スクリプト: `/tmp/integration_golden.py`, `/tmp/live_smoke.py`, `/tmp/live_smoke2.py`, `/tmp/perf_bench.py`。

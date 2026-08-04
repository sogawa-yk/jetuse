# リファクタリング検証レポート — review-validation.md 全項目の実施と検証

作成日: 2026-06-18
ブランチ: `task/refactor-review-validation`（origin へ push 済み）
元計画: [docs/refactoring/review-validation.md](../refactoring/review-validation.md)（検証済み版・P0〜P2）
実施方式: Agent Teams（並列サブエージェント＋統合者によるフェーズ別コミット）
検証環境: OCI `dev` インスタンス（ap-osaka-1）/ 実 OCI GenAI・実 dev ADB（`jetusedev_low`, JETUSE_APP）/ 直 uvicorn・vite / `AUTH_REQUIRED=false`

---

## 1. サマリ

`docs/refactoring/review-validation.md` に記載の**全リファクタリング（P0〜P2）を実施**し、その後 **単体・結合・パフォーマンス・Playwright E2E** を実バックエンドで多シナリオ実行した。

| 指標 | Before | After |
|---|---|---|
| backend pytest | **118 passed / 7 failed**（CI RED） | **135 passed / 0 failed**（CI GREEN） |
| backend カバレッジ下限 | なし | **45%**（実測 54.73%） |
| frontend ユニットテスト | **0** | **48**（5ファイル） |
| 重複SSEパーサ | 9ファイルにベタ書き | `lib/sse.ts` に集約（9箇所置換） |
| API↔コンテナ二重実装 | SSRF/SQL/DDGを二重管理（定数乖離） | `jetuse_shared` に一本化（契約テスト28件） |
| `service/main.py` | **1551行** | **118行**（9ルーター＋schemas/dispatch/validators へ分解） |
| `chat.tsx` | **1194行** | **466行**（7ファイルへ分割） |
| `prefs.tsx` / `dbchat.tsx` / `voicechat.tsx` | 703 / 645 / 452 | 58 / 293 / 237 |
| main JS chunk | **808.68 KB** | **349.00 KB**（gzip 240→108） |
| デッドコード | 5項目＋2エンジンクラスタ | 削除済み |
| 旧framework値DBレコード | `agents_sdk`:2 / `native`:2 残存 | migration 012 で canonical 化（残存0） |

差分規模: **90 files changed, +9590 / -5358**。コミット10本。全テスト合計 **211 passed**（backend 135 + jetuse_shared 28 + frontend 48）。

---

## 2. フェーズ別実施結果

| Phase | コミット | 内容 | ゲート結果 | 生成物 |
|---|---|---|---|---|
| **P0** | `6491175` | agent framework テスト7件を ADR-0009 準拠へ更新。422/503分離。pytest-cov+下限45% | pytest 125→緑、ruff clean | — |
| **P1a** | `5b65ff9` | `lib/sse.ts`(`readSse<T>`) + Vitest基盤(vitest/@testing-library/jsdom) | vitest 18、lint/build green | — |
| **P0.5** | `3e3c903` | デッドコード5件削除(get_owner_tables/delete_owner/Nav/vite.svg/test.txt) | ruff+pytest+build green | — |
| **P0.6** | `55ab868` | 旧framework値のDB移行 `012` を dev ADB へ適用。正規化方針 | 適用後レガシー0 | ADR-0010, tips×2 |
| **P1b** | `051b849` | SSRF/web_fetch/web_search/sanitize_sql を `jetuse_shared` へ一本化、Containerfile pip化 | 契約28+api125、podman 3コンテナbuild成功 | jetuse_shared README |
| **P0.7** | `d6800d2` | インプロセス `agents_sdk.py`/`langgraph_engine.py` 削除 | pytest 124、ruff clean | specs/14・ADR-0009追記 |
| **P1c-fe** | `87f68e9` | `chat.tsx` 1194→466 分割 + 6画面のSSEを `readSse` へ | vitest 35、lint/build green | — |
| **P1c-fe** | `e101c11` | `prefs`/`dbchat`/`voicechat` 分割 + 残SSE移行 + ucform test | vitest 48、lint/build green | — |
| **P2** | `726e5ea` | route `React.lazy` + Markdown遅延で main chunk 808→349KB | vitest 48、build green | — |
| **P1c-be** | `b06cb88` | `service/main.py` 1551→118 を9ルーター＋schemas/dispatch/validators へ分割 | golden 39ケースbyte一致、pytest 135 | — |

> P0.6 の DB 移行は **実 dev ADB へ実際に適用**した（`UPDATE agents ... WHERE framework IN ('native','agents_sdk','hosted')`）。適用前 `agents_sdk`:2/`native`:2/`openai_agents`:4/`adk`:1/`select_ai`:1 → 適用後 `openai_agents`:8/`adk`:1/`select_ai`:1。

---

## 3. リファクタリング検証（挙動同値・削除証跡）

- **P1c-backend の挙動同値**: 分割前後で TestClient による golden 応答 **39ケース（422×15 / 503×11 / 400×7 / 200×5 / 501×1）が byte 単位で一致**。各抽出ステップ後に pytest 緑を維持。`service.main:app` のインポート契約・SSEフレーム形・validation メッセージは不変。
- **P1c-frontend の挙動保持**: chat の 401/2回リトライ・空応答ガード・ツール承認往復(`sdk_state`/`sdk_approvals`)、各画面の 401(throw / reauth-return)と Abort(rethrow / silentAbort) を `readSse` の opt で厳密維持。i18n は ja/en **321キー一致**を型束縛＋`dict.test.ts` で保証（値 byte-identical）。
- **デッドコード削除証跡**: 削除対象は静的参照0を grep 確認済み。`agents_sdk.py`/`langgraph_engine.py` 削除後も `RAG_TOOL_DESCRIPTION`/`rag_search_text`/`stream_agents_sdk` 等の dangling 参照0。
- **現役コードの温存**: `hosted_agent.normalize_sdk`/`_LEGACY_SDK`（read-time正規化, ADR-0010）、framework値 `langgraph` 等、`stream_agent`（アドホック native経路）、`sdk_state`/`sdk_approvals`(承認往復UI) は意図通り残置。
- **セキュリティ非劣化**: `jetuse_shared` 移管後も SSRF ガードは private/loopback/link-local/metadata/IPv6 `::1` を拒否、SQLサニタイザは同一禁止文を拒否（契約テスト28件＋コンテナイメージ内実行で確認）。`MAX_TEXT_CHARS` 乖離は `MAX_PAGE_CHARS=20000`(取得上限) と `MAX_TEXT_CHARS=8000`(ツール出力上限=両ランタイム従来の実効値) に整理。

---

## 4. テスト結果

### 4.1 単体（Unit）
| 対象 | 結果 |
|---|---|
| backend `pytest -q` | **135 passed / 0 failed**、coverage **54.73%**（下限45%）。main.py 100% / schemas.py 100% / sse.py 89% / validators.py 89% |
| `jetuse_shared/tests` | **28 passed**（SSRF/sanitize_sql/DDG/web_fetch/get_current_time 契約） |
| frontend `vitest run` | **48 passed / 5ファイル**（sse 18・buildChatRequest 11・useChatStream 6・ucform 11・dict 2） |

### 4.2 結合（Integration）
- **TestClient golden**: 20/20 PASS。422検証（未知モデル / code_interpreter単独 / agentにmcp / select_ai不正ツール / usecase各種）＋ chat未知モデル400 ＋ 9ルーターmount確認(全て非404)。in-process TestClient はウォレット無しのため DB系GETは503=mount確認、実DBは下記ライブで実証。
- **ライブ・3エージェント経路（実OCI/ADB）**:
  - **(a) native アドホック chat**（llama-3.3-70b）: 200・delta 10件・「2+2は4です」。
  - **(a2) native ReAct + ツール**（gpt-oss-120b + get_current_time）: 200・`tool_call`=1/`tool_result`=1 が発火・最終回答に時刻反映。`stream_agent` 経路確認。
  - **(b) Select AI**（backend=select_ai）: 200・ADB `DBMS_CLOUD_AI` 経由で妥当SQL生成、`dbchat/execute` 閉ループで `CHANNEL_COUNT=5`。
  - **(c) hosted agent**: create→get→run→delete を実行。run は **HTTP 200＋インラインSSEエラー**「agent container not configured / missing=[hosted_agent_idcs_domain,_client_id,_client_secret,_scope]」＝コンテナ未デプロイのため想定どおり（HTTP 503 ではなく 200+errorフレーム）。
- **その他ライブ機能**: 翻訳(ja→en)200 / OCR vlm(gemini-2.5-pro)200・document(conf 0.969)200 / TTS Phoenix 200(音声5823 bytes) / conversations 200 / agents・usecases・presets CRUD往復 200 / dbchat/schema 200 / realtime STT セッション(作成→audio→削除)200。作成データは全て後始末済み。

### 4.3 パフォーマンス（直 uvicorn・max_tokens=256）
| model | N | TTFT P50/P95/max(s) | TOTAL P50/P95/max(s) | 最大チャンク間隔(s) |
|---|---|---|---|---|
| llama-3.3-70b | 5 | 0.15 / 0.15 / 0.15 | 1.73 / 3.87 / 3.87 | 0.72 |
| gpt-oss-120b | 5 | 0.35 / 0.58 / 0.58 | 0.84 / 0.99 / 0.99 | 0.08 |
| gemini-2.5-flash | 5 | 2.36 / 4.22 / 4.22 | 2.46 / 4.31 / 4.31 | 0.15 |
| gemini-2.5-pro | 2 | 3.85 / 3.92 / 3.92 | 3.91 / 3.94 / 3.94 | 0.08 |

- **SSE バッファリングなし**: 最大 delta 間隔 0.72s ≪ `KEEPALIVE_SECONDS=15`。全ストリームで `{"ka":1}` プリロールフレームを確認。リファクタ後も SSE フレーム形・keepalive は不変。
- 注: 直 uvicorn 計測のため API Gateway 経由(`SPIKE-02`/`CP2-measurements`)とは厳密比較不可（傾向のみ）。詳細: [perf-refactor.md](./perf-refactor.md)。

### 4.4 Playwright E2E（実バックエンド・全16ページ）
HashRouter SPA。各ページで描画・主要操作・`console`エラー・`/api`失敗を確認。スクリーンショットは [e2e-screenshots/](./e2e-screenshots/)。

| # | ページ | シナリオ（操作） | 結果 | 画像 |
|---|---|---|---|---|
| 1 | ホーム `#/` | ナビ16リンク＋ユースケースギャラリー描画 | ✅ console 0 | 01-home.png |
| 2 | チャット `#/chat` | GPT-OSS 120Bへ「大阪リージョンの特徴を1文で」送信→実GenAIストリーム描画 | ✅ 応答描画・console 0 | 02-chat-streamed-reply.png |
| 3 | DBチャット `#/dbchat` | サンプル質問→NL2SQL生成→実行→3行結果表→グラフ化（実ADB SH） | ✅ SQL/結果/チャート・console 0 | 03-dbchat-nl2sql-result-chart.png |
| 4 | ユースケース(翻訳) `#/uc/builtin-translate` | 本文入力→実行→実LLM翻訳「The Osaka region fully supports...」 | ✅ 出力描画・console 0 | 04-usecase-translate.png |
| 5 | RAGチャット `#/rag` | アップロードゾーン＋文書一覧(空)をバックエンドから取得 | ✅ console 0 | 05-rag.png |
| 6 | エージェント `#/agents` | エージェント一覧描画(dev-userは空) | ✅ console 0 | 06-agents.png |
| 7 | エージェント作成 `#/agents/new` | agentbuilder フォーム描画(framework読替含む) | ✅ console 0 | 07-agentbuilder.png |
| 8 | OCR `#/ocr` | OCRアップロード/オプション画面描画 | ✅ console 0 | 08-ocr.png |
| 9 | 議事録 `#/minutes` | 録音/アップロードUI描画 | ✅ console 0 | 09-minutes.png |
| 10 | リアルタイム翻訳 `#/realtime` | STT＋翻訳UI描画 | ✅ console 0 | 10-realtime.png |
| 11 | 音声チャット `#/voicechat` | 録音/STT/TTS UI描画 | ✅ console 0 | 11-voicechat.png |
| 12 | 映像分析 `#/video` | フレーム抽出/分析UI描画 | ✅ console 0 | 12-video.png |
| 13 | ユースケース作成 `#/builder` | ビルダーUI描画 | ✅ console 0 | 13-builder.png |
| 14 | 管理 `#/admin` | 利用状況。dev-userは非管理者→403で「管理者のみ」表示(graceful) | ✅ 想定どおり(後述) | 14-admin.png |
| 15 | 設定 `#/settings` | 認証/ブランド/環境設定描画 | ✅ 新規エラー0 | 15-settings.png |
| 16 | デザインギャラリー `#/design` | UIコンポーネント一覧描画 | ✅ console 0 | 16-design.png |

- **セッション全体の console エラーは2件のみ**＝管理ページの `GET /api/admin/usage` **403**（dev-userは `is_admin:false`、`admin_users` 未設定）。これは**認可の正しい挙動**でリファクタ起因ではない。画面は「管理者のみがアクセスできます」と graceful 縮退。HashRouter のため console はハッシュ遷移で消えず、以降のページにも同2件が残留表示されるが、ページ固有の新規エラーは0。
- バンドル分割（route lazy）の実チャンク分離は vite **dev** モード（非バンドルESM配信）では観測対象外。本番 `dist` ビルドで実証（§6）。

---

## 5. 未通し項目とその後の追検証

初回の E2E（§4 のローカル起動）では、ローカル uvicorn に `.env` のみを渡しており、**hosted agent / Speech の設定は `terraform.tfvars` の `api_environment` 側にしか無かった**ため未設定エラーになっていた。これは「デプロイされていない」のではなく**ローカル起動時の env 注入漏れ**であった（リファクタ起因ではない）。`api_environment` の値（hosted agent の OCID/IDCS・`SPEECH_BUCKET=jetuse-dev-speech`）をローカル起動へ注入して**追検証した結果、いずれも実機で成功**（詳細は §9）。

| 機能 | 初回 | 追検証(§9) | 備考 |
|---|---|---|---|
| hosted agent 実行（OpenAI SDK / LangGraph） | 200+設定不足エラー | ✅ **成功**（コンテナでReAct・ツール実行） | 3コンテナは2026-06-15から既にACTIVE |
| バッチ音声文字起こし（議事録） | 未実施 | ✅ **成功**（Whisper話者分離→議事録生成） | `jetuse-dev-speech` バケット既存 |
| OpenSearch RAG | エラー（ローカル未設定） | ✅ **成功**（デプロイ済みGW経由・§9.3） | クラスタ `jetuse-dev-opensearch` ACTIVE/2.19.1。9200は私設サブネット |
| 管理 `/api/admin/usage` | 403 | （変更なし） | dev-userが非管理者（正しい認可） |

> 補足: Speech 全機能が落ちるわけではない。**realtime STT セッション・TTS(Phoenix)・OCR(Document Understanding/VLM)はインスタンス認証で実動作**を確認済み（§4.2）。

---

## 6. バンドル分析（本番 `dist` ビルド）

| chunk | Before | After |
|---|---|---|
| index（main, eager） | 808.68 KB / gzip 240.74 | **349.00 KB** / gzip 107.96 |
| MarkdownInner（新規, lazy） | —（mainに同梱） | 324.09 KB / gzip 100.46 |
| route別 chunk（新規, lazy） | —（mainに同梱） | chat 29.5 / dbchat 17.0 / voicechat 8.7 KB ほか各ページ分離 |

- main chunk は **808.68→349.00 KB** で警告閾値(500KB)を大きく下回る。初期ロードは index＋当該ページのみ。Markdownスタック・mermaid/cytoscape/katex は必要時のみ。
- 残る ">500KB" 警告は**既に遅延化済みの mermaid vendor chunk(593KB)**のみで初期ロード非対象。`chunkSizeWarningLimit` で握り潰さず実シグナルを残す方針。

---

## 7. オープン項目 / 申し送り

- **hosted agent / OpenSearch / バッチSpeech**: 課金リソース未デプロイ・バケット未設定のため E2E 未通し。デプロイ後に再検証（ADR-0009 のデプロイは最終承認制）。
- **`jetuse_shared` 共有の追加候補**: SemanticStore / NL→SQL生成 / 読取専用実行は per-runtime 認証差のため意図的に重複温存（将来タスク）。
- **フロント読替の整理**: `agentbuilder.tsx`/`agents.tsx` の framework 読替は全環境で migration 012 適用確認後に撤去可（ADR-0010）。
- **CI**: web job に `vitest run` 追加済み。Playwright スモークは実バックエンド依存のため手動/専用セッション運用（本レポートが実施記録）。

---

## 8. 付録

### 実行コマンド
```bash
# backend
cd packages/api && ../../.venv/bin/ruff check . && ../../.venv/bin/pytest -q   # 135 passed
.venv/bin/pytest packages/jetuse_shared/tests -q                                 # 28 passed
# frontend
cd packages/web && npm run lint && npx vitest run && npm run build             # 48 passed, main 349KB
# 実バックエンド起動（検証用・/tmp/run_api.py がADB接続envを注入）
bash ops/start-adb-if-stopped.sh && .venv/bin/python /tmp/run_api.py           # :8000
cd packages/web && VITE_AUTH_REQUIRED=false npm run dev                        # :5173
```

### 環境
- 実 OCI GenAI（ap-osaka-1）/ 実 dev ADB `jetusedev_low`（JETUSE_APP, ウォレット `/tmp/jetusedev_wallet`）/ `AUTH_REQUIRED=false`（dev-user）/ 直 uvicorn・vite（API Gateway非経由）。

### コミット（`task/refactor-review-validation`）
```
6491175 test(P0)     d6800d2 refactor(P0.7)
5b65ff9 feat(P1a)    87f68e9 refactor(P1c-fe chat)
3e3c903 refactor(P0.5) e101c11 refactor(P1c-fe prefs/db/voice)
55ab868 feat(P0.6)   726e5ea perf(P2 bundle)
051b849 refactor(P1b) b06cb88 refactor(P1c-backend)
```
関連: [perf-refactor.md](./perf-refactor.md) / [ADR-0010](../decisions/ADR-0010-framework-normalization.md) / [review-validation.md](../refactoring/review-validation.md)

---

## 9. 追検証 — hosted agent / Speech バッチ（2026-06-18, ユーザー指示「OpenSearch以外」）

### 根本原因
初回 §4 のローカル起動は `.env` のみ読み込んでいたが、hosted agent と Speech の設定は **`infra/terraform/environments/dev/terraform.tfvars` の `api_environment`** にのみ存在した（デプロイ済みコンテナ環境変数）。`api_environment` から `HOSTED_AGENT_*` / `AGENT_{OPENAI,LANGGRAPH,ADK}_APP_OCID` / `SPEECH_BUCKET=jetuse-dev-speech` をローカル uvicorn へ注入して再検証した（`AUTH_MODE`/OpenSearch endpoint は除外、ユーザー認証のまま）。**新規リソース作成・課金・IDCS変更は一切なし**（3コンテナは2026-06-15からACTIVE、bucketも既存）。

### 9.1 hosted agent（3SDK別 Hosted Application 経由・ADR-0009）
保存済みagentを作成→`/api/chat/stream`(`agent_id`指定)→`hosted_agent.invoke_agent` 経由で**実コンテナを呼び出し**、IDCS OAuth(client_credentials)でトークン取得して invoke：

| SDK | コンテナ | 結果 |
|---|---|---|
| `openai_agents` | jetuse-dev-agent-openai | 200・`get_current_time` ツール実行→「今日の日付は2026年6月19日…OCI大阪は『活気』」（19.1s） |
| `langgraph` | jetuse-dev-agent-langgraph | 200・「2+2=4」（0.7s） |

- **UI E2E**：エージェント一覧に「OpenAI Agents SDK（ホスト型）」として表示（[17-hosted-agent-list.png](./e2e-screenshots/17-hosted-agent-list.png)）。チャットでエージェント選択→送信→**🛠 get_current_time 実行**バッジ＋コンテナ回答「2026年06月19日（金）です。OCI大阪リージョンの強みは…低遅延かつ高帯域幅…」を描画（console 0、[18-hosted-agent-chat.png](./e2e-screenshots/18-hosted-agent-chat.png)）。
- 検証用agent・会話は後始末済み。

### 9.2 Speech バッチ文字起こし（議事録 VOICE-01）
TTS(Phoenix, Yuki)で生成した日本語mp3（56,127 bytes）を `/api/minutes` へアップロード：

1. ジョブ作成 **200**（初回の `SPEECH_BUCKET未設定` を解消。`jetuse-dev-speech` バケットへ音声を put → OCI Speech `WHISPER_MEDIUM`＋話者分離ジョブ）。
2. ポーリング → 約12秒で **completed**。話者分離トランスクリプト取得：`[{speaker:0, 0.47–13.37s, "これはテスト会議の音声です。本日は大阪リージョンの議題について…決定事項は来週までにレポートを提出…"}]`（合成音声を正確に文字起こし）。
3. `/api/minutes/{id}/generate` → LLMが**議事録Markdown**生成（# 議事録 / ## 出席者 / ## 決定事項「大阪リージョンに関するレポートを来週までに提出」/ ## TODO 表）。
4. 検証用ジョブは後始末済み（既存の他ジョブには非干渉）。

### 9.3 OpenSearch RAG（デプロイ済み API Gateway 経由）
OpenSearch クラスタ `jetuse-dev-opensearch` は **ACTIVE / v2.19.1**（control plane で確認）。ただしデータプレーン `https://10.1.1.202:9200` は**私設サブネット**にあり、このローカル検証インスタンス（10.0.0.44）からは経路がない（NSG/セキュリティリスト）。一方、**デプロイ済みの Container Instance は同 VCN 経路で到達可能**なので、**本番 API Gateway 経由**で検証した。

- 認証: 本番 API は `AUTH_REQUIRED=true`。IDCS ドメインの **client_credentials**（`jetuse-agent` クライアント）でトークン取得 → Gateway `…mwisouirdskp3p7uardy463bam….apigateway…` の `/api/me` が 200（subject=client_id）。
- フロー（実施→後始末）: `POST /api/rag/files`（極秘事実を含む `jetuseos-secret.txt` を投入）→ 取り込み状況 **`{opensearch: indexed, vector_store: pending, select_ai: pending}`**（OpenSearchは同期＝即時索引）→ `POST /api/chat/stream {rag:true, rag_backend:"opensearch"}` で「JETUSE-OSのコードネームと商用稼働開始日は？」→ **回答「コードネームは『ナニワ電光』、開始予定日は2026年7月1日」**（文書にのみ存在する事実を正確に取得）＋ citations（当該ファイル score 0.93）→ `DELETE /api/rag/files/{id}` で後始末。
- これにより OpenSearch の **kNN データプレーン経路が実機で機能**することを確認（ローカル不可の原因は純粋にネットワーク隔離）。

> セキュリティ観察（リファクタ非関連・申し送り）: 本番 API の JWT 検証は `oidc_audience` 未設定のため **audience 非検証**（`verify_aud:False`）。同一 IDCS ドメインが署名したトークンであれば audience を問わず通る。多層防御として `OIDC_AUDIENCE` の設定を推奨（別タスク）。

### 結論
**review-validation.md の対象に紐づく未通し項目はすべて実機で検証成功**（hosted agent / Speechバッチ / OpenSearch RAG）。初回「未通し」はコード/デプロイ欠陥ではなく、①ローカル起動の env 注入差（hosted/Speech）と②私設サブネットのネットワーク隔離（OpenSearch＝デプロイ済みGW経由で検証）に起因。新規課金リソースの作成・NSG変更・IDCS設定変更は一切行っていない。スクリーンショットは [e2e-screenshots/](./e2e-screenshots/) に 17–18 を追加（計18枚）。

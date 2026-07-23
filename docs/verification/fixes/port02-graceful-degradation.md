# PORT-02 グレースフルデグレード検証レポート（機能別環境依存の表面化）

- 日付: 2026-07-13
- 対象: FIX-47(genai fail-fast / `/api/rag/health`)を土台に、GenAI以外の機能面
  (モデル可用性・NL2SQL・Speech・OCR・TTS・観測)へ縮退を広げるPORT-02タスク
- 環境: jetuse:test 共有E2Eスタック `jetuse-spike-fix47`(us-chicago-1。詳細は
  `/home/opc/jetuse-loops/fix47-e2e-shared/README.md`)。専用タグ
  `<ocir-region>.ocir.io/<namespace>/jetuse-api:port02-e2e` を投入し `api_image_url` のみ更新して apply
  (実エンドポイント値は非公開の `runs/2026-07-13T0730_PORT-02/e2e/` 証跡側のみに記録)
- 結論: **静的検証(codex-review PASS, blocker 0) + 実環境E2E 3シナリオ合格 + 1シナリオ部分実施
  (理由記録)で完了条件を満たす**

## 1. 実装した8項目(タスク仕様どおり)

| # | 項目 | 主な変更 |
|---|---|---|
| 1 | モデル可用性の反映 | `jetuse_core/models.py`: lazy mark(TTL 5分、読み取り専用)。`chat.py`: 401/403/404で`mark_unavailable`(RAG/会話メモリ/project絡みは対象外に限定)。`routes/chat.py`: `GET /api/chat/models`に`available`フラグ、既定モデル不可時のchat-familyフォールバック |
| 2 | dbchatの既定切替 | `routes/dbchat.py`: SEMSTORE_OCID未設定でselect_aiへ自動切替(web UIが常にbackendを明示送信するため「未指定」と区別不可 — 到達性を優先。詳細下記§4) |
| 3 | Select AIの可視化 | `bootstrap.py`: `resource_principal_status()`(ok/None/hint)。`nl2sql.py`: `create_profile`失敗にIAMヒント付きエラー |
| 4 | Speech/OCR/TTSの縮退 | `tts.py`: `TtsError`(Phoenix未購読ヒント)。OCR/Speechは既存の縮退(FIX-47以前から)を維持 |
| 5 | SH sampleの検査 | `nl2sql.py`: `sh_sample_status()`。`routes/dbchat.py`: 生成前検査+SSEエラー正規化 |
| 6 | obsの抑制 | `obs.py`: 指数バックオフ+バッファ保持(初回失敗分も破棄しない)+ N回に1回のサマリ + 401/403でdetach |
| 7 | AUTH_MODEガード | `genai.py`: `load_local_oci_config()` + `_signer()`自体もラップ(OciUserPrincipalAuth内部呼び出し分)。14+1箇所差し替え |
| 8 | healthの拡張 | `jetuse_core/health.py`新設・`GET /api/health`: chat(モデル別)/rag/dbchat/speech/ocr/ttsを`ok/degraded/unavailable`+hintで構造化。副作用なし(project自動作成を起こさない) |

## 2. 静的検証

- `.venv/bin/pytest packages/api/tests`: **325 passed**
- `.venv/bin/ruff check packages/api`: **All checks passed**
- codex-review: review-1〜14(`runs/2026-07-13T0730_PORT-02/reviews/`)。ラウンド2〜13で
  11件のblocker/majorを修正(untracked file未添付・agent_idのフォールバック汚染・
  RAG誤爆・health診断の副作用・dbchat到達性・Functionsルーター二重実装ドリフト・
  KeyError等)。**review-14でPASS(blocker=0, major=2)** に到達し、loop-protocol 5.5の
  停止規律により以降のmajor/minorは追わずresidualとしてSTATE.mdに列挙して打ち切り。

## 3. 実環境E2E(2026-07-13実施。証跡: `runs/2026-07-13T0730_PORT-02/e2e/`)

デプロイ: `podman build/push` → `oci resource-manager stack update --variables`(`api_image_url`
のみ) → `create-apply-job`(SUCCEEDED, 2026-07-13T09:39:48Z) → `fix_wallet_and_restart.sh`
(APPLY_JOB指定・ウォレット復旧+CI再起動・DB READYまで75秒+初回試行で200)。

### シナリオ1: モデル可用性 — 合格
`GET /api/chat/models`が5モデル全件に`available: true`を返し(scenario1-models.body)、
実チャット`POST /api/chat/stream`(gpt-oss-120b)が実応答("東京。" — scenario1-chat.sse)、
未登録モデル指定が生エラーでなく`400 unknown model`(scenario1-unknown-model.body)。

### シナリオ2: dbchat縮退 — 合格(coreパス)。schemaフィールドは部分未実施(理由: SKIPPED.md)
このスタックは`SEMSTORE_OCID`が実際には未到達(`/api/health`のdbchat.semantic_store.ok=false)
という「別テナンシで機能未整備」を自然に再現していた。`POST /api/chat/nl2sql`
(backend省略=実UIと同じペイロード)がselect_ai経路へ自動切替し、SH.SALES/SH.TIMESに対する
実SQLを正しく生成(scenario2-nl2sql-generate.body)。完了条件「SEMSTORE_OCID未設定の
クリーンルームでdbchat既定がselect_ai経路で応答する」を実環境で達成。

### シナリオ3: health集約 — 合格
`GET /api/health`(scenario3-health.body)がchat(5モデル)/rag/dbchat/speech/ocr/ttsを構造化し、
dbchatが`semantic_store`未構成・`select_ai`未検証(理由付き)・`sample_data`読み取り可、という
3つの異なる状態を同時に区別して報告。コンテナログの`bootstrap`再試行ループ
(scenario4-container.log)と`select_ai.ok=null`(未検証)が整合し、「未確認をokと偽らない」
(F-003修正)を実データで裏付け。

### シナリオ4: obs抑制 — 未実施(IAM意図的剥奪は人間ゲート。単体テスト証跡で代替)
詳細は `runs/2026-07-13T0730_PORT-02/e2e/SKIPPED.md`。

## 4. E2E中の新発見(docs/tips.mdへ反映済み)

1. **`oci.config.from_file()`直呼びはgenai系の入口(`_signer()`/`OciUserPrincipalAuth`)にも潜む**:
   サードパーティラッパーが内部で独自に設定ロードするケースはgrep一発では見つからない。
2. **dbchatのbackend選択はweb UIが常に明示送信し「未指定」を表現できない**: APIスキーマだけで
   既定値を区別する設計はフロントの初期state送信パターンを壊しうる(blocker→revert)。
3. **OCI Functionsルーター(fn/router/func.py)はADR-0005で二重実装禁止だが機械的に検出できない**:
   `/api/dbchat/*`・`/api/tts/*`・`/api/presets/*`はFastAPIと**別デプロイ**のOCI Functions
   (`ORACLE_FUNCTIONS_BACKEND`)へルーティングされ、CI側だけ直しても実環境には反映されない。
   本E2Eで実際に`sample_available`フィールドが欠落する形で再現・確認(§3シナリオ2)。
   Functionsイメージ(`jetuse-fn-router:latest`)の更新は`:latest`禁止のため人間ゲート・
   後続タスク。

## 5. 残る人間ゲート

- コミット/PR/push(本セッションでは未実施)
- Issue #47返信コメント案への追記(自己診断手順=拡張healthの使い方)
- `jetuse-fn-router`イメージの新タグpush(§4-3。Functions側のADR-0005追従)
- 共有E2Eスタック(`jetuse-spike-fix47`)は稼働状態のまま残置。destroyはキュー後始末
  (tasks/FIX47-PROGRESS.md)で人間が判断

# PORT-02 E2E — 未実施範囲と理由

## 実施した4シナリオ(tasks/PORT-02.md記載の4本すべて着手。うち3本は完全な実環境証跡、
1本は部分実施+理由記録)

## 1. モデル可用性 — 完全実施
`GET /api/chat/models`(scenario1-models.body)・`POST /api/chat/stream`実チャット
(scenario1-chat.sse, "東京。"が返る)・未知モデルの400(scenario1-unknown-model.body)を
実環境(jetuse-spike-47スタック, us-chicago-1)で確認。`/api/chat/{p*}`はAPI GWのHTTP_BACKEND
(CI)経由で今回ビルドしたport02-e2eイメージが処理していることをコンテナログ(scenario4-container.log)
のパス一致で確認済み。

## 2. dbchat縮退 — 完全実施(coreパスは`/api/chat/nl2sql`、CI経由)
このスタックは`enable_semantic_store=true`だが実際のコンテナには`SEMSTORE_OCID`が
未到達(`/api/health`のdbchat.semantic_store.ok=false, hint="SEMSTORE_OCID 未設定" —
scenario3-health.body)という、まさに「別テナンシで機能未整備」を体現するクリーンルーム状態が
自然に存在した。`POST /api/chat/nl2sql`(target=sample, backend省略=UI既定と同じペイロード)で
select_ai経路へ自動切替され、SH.SALES/SH.TIMESに対する実SQLが正しく生成されることを確認
(scenario2-nl2sql-generate.body)。これはtasks/PORT-02.mdの完了条件「SEMSTORE_OCID未設定の
クリーンルームでdbchat既定がselect_ai経路で応答する」を実環境で満たす。

**部分未実施**: `GET /api/dbchat/schema`の新フィールド(`sample_available`)は実環境応答に
含まれなかった(scenario2-schema.body)。原因を特定: このスタックのAPI Gatewayは
`/api/dbchat/{p*}`・`/api/tts/{p*}`・`/api/presets/{p*}`をCIでなく**別デプロイのOCI Functions**
(`ORACLE_FUNCTIONS_BACKEND`、terraform state実測で確認)へルーティングしており、Functionsの
イメージは`<ocir-region>.ocir.io/<namespace>/jetuse-fn-router:latest`に固定(`api_image_url`変数とは
無関係の別リソース)。今回のタスク指示は「stack変数`api_image_url`のみ更新」であり、
Functions側イメージの更新は指示範囲外。加えてCLAUDE.mdの禁止事項「OCIR `:latest` タグへの
push」によりこのFunctionsイメージの更新自体が人間ゲート事項(このE2Eでは実施不可)。
`fn/router/func.py`側の対応する修正(`/api/dbchat/schema`にsample_available付与、
`/api/tts`のTtsError捕捉)は実装済み・`tests/test_fn_router.py`で単体テスト済み
(4件: test_tts_error_surfaces_hint_as_503, test_dbchat_schema_reports_sample_available,
test_dbchat_schema_reports_sample_unavailable_reason 等)。Functions実イメージ更新は
後続タスク(人間承認のうえ`jetuse-fn-router`イメージの新タグpush)で実施が必要
(docs/tips.md 2026-07-13「Functionsルーターの二重実装ドリフト」に記録済み)。

`POST /api/tts`自体は実環境で200・実MP3音声(6.6KB, MPEG ADTS layer III)を確認
(scenario-tts.body)。これは旧(未更新)Functionsコードでの応答のため、TTSの新縮退
(TtsError統一)の実証ではなく、既存機能の非破壊(regressionが無いこと)の確認として記録する。

## 3. health集約 — 完全実施
`GET /api/health`(scenario3-health.body)で6機能(chat/rag/dbchat/speech/ocr/tts)が
構造化されて返り、dbchatが実際に`semantic_store`未構成・`select_ai`未検証(理由付き)・
`sample_data`読み取り可、という3つの異なる状態を同時に区別して報告することを確認。
コンテナログでも`bootstrap`が`ADB 未準備のため20s後に再試行`を繰り返しており(スタック側の
既知事象・PORT-02の対象外)、`select_ai.ok=null`(未検証)が実際に「未確認」であって
「確認済みでok」と偽っていないことを裏付ける一致が取れた(health側の値とコンテナログの
実状態が整合)。

## 4. obs抑制 — 未実施(理由: 意図的な権限剥奪は人間ゲート)
OCI Logging/MonitoringへのIAM権限を意図的に外す操作はテナンシのポリシー変更を伴い、
CLAUDE.mdの人間ゲート対象(IAMポリシー変更)にあたるため、共有E2E環境で実施しなかった。
代替として単体テスト証跡(tests/test_obs.py、8件: `_ShipThrottle`のバックオフ/認可エラー
detach、`_retain_buffer`のバッファ保持、実スレッドでの1回目失敗バッチ保持の回帰テスト
`test_log_worker_retries_first_failed_batch_instead_of_dropping_it`)で検証済み。
実環境のコンテナログ(scenario4-container.log)では、通常運用下でログ送信が
`"logger": "jetuse.obs", "message": "oci logging attached"`のみで、送信失敗のスパムは
発生していないことを確認(正常系での非スパムを実環境ログで裏付け)。

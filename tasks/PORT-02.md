# タスク: PORT-02 ランタイムのグレースフルデグレード（機能別環境依存の表面化）

## 目的
別テナンシで「サービス / リソース未整備の機能」が生の 500 / SSE 生エラー文字列 / サイレント故障で
死ぬのを止め、**原因ヒント付きで機能単位に安全に縮退**させる。FIX-47 が入れる genai fail-fast と
`/api/rag/health` の考え方を、GenAI 以外の機能面（モデル可用性・NL2SQL・Speech・OCR・TTS・観測）へ
広げる。Issue #47 の「切り分け不能」問題のアプリ全域での根治。

## 事前調査で確定済みの事実（2026-07-10 監査。file:line 検証済み・再検証不要）
- **モデル可用性**: レジストリ（jetuse_core/models.py:24-48）はハードコードで、リージョン/
  テナンシに無いモデルを選ぶと chat.py:573-577 が provider の生エラーを SSE `{"error":...}` で
  返すだけ。`GET /api/chat/models`（routes/chat.py:480-496）は無条件に全モデルを列挙し UI で
  選択可能に見える。モデルのリージョン別提供差は実在（CLAUDE.md: Grok/Llama4 は大阪不可 等）。
- **dbchat 既定バックエンドは semantic store**（routes/dbchat.py:138-139）: SEMSTORE_OCID
  未設定なら nl2sql.py:56-57 が RuntimeError → SSE 生エラー。公開 ORM スタックは semantic store
  を作らないので**別テナンシの既定 dbchat は必ず壊れる**（select_ai バックエンドは動きうるのに）。
- **Select AI の RP 有効化は best-effort**（bootstrap.py:106-116 — 失敗しても警告のみ）:
  失敗すると CREATE_PROFILE / GENERATE が後段で全部 SSE 生エラー・502 になり原因が見えない。
- **Speech（minutes batch + realtime STT）/ Document Understanding**: `oci_region` にサービスが
  ある前提 + IAM 前提（minutes.py:44-58, stt_realtime.py:104-110, docunderstand.py:78-86）。
  未整備時は「IAM 未整備の可能性」程度の 503 か生 502。
- **TTS は us-phoenix-1 固定**（settings.py:99。TTS 自体が Phoenix 限定というサービス制約は正）:
  Phoenix **未購読テナンシ**では 404→503 になるが、購読が原因と分からない。TTS_REGION は
  設定にあるが ORM 未配線・ヒントなし。
- **dbchat sample ターゲットは ADB 同梱 SH スキーマの PUBLIC 公開依存**（nl2sql.py:214、
  bootstrap は SH への grant をしない）: SH が読めない ADB では ORA-00942 → 400、サンプルタブは
  空表示のサイレント故障。
- **obs の ship 失敗 spam**（obs.py:80-97 log / 157-164 metrics）: 失敗のたび stderr 1 行・
  バックオフなし = Issue #47 コンテナログの「oci log ship failed」洪水の正体。実ログを埋める。
- **AUTH_MODE 未設定コンテナは ~/.oci/config を探して未処理クラッシュ**: `oci.config.from_file()`
  フォールバックが genai.py:24 ほか計 9 箇所（obs/tts/stt_realtime/translate/guardrails/
  docunderstand/embeddings/minutes）。ORM は AUTH_MODE を配線済みだが、手動/部分デプロイで
  欠けると ConfigFileNotFound → 500。
- 参考（既に正しく縮退するもの・変更不要）: translate の LLM フォールバック（translate.py:87-98）、
  guardrails / moderation の fail-open、OpenSearch バックエンドの self-disable
  （rag_opensearch.py:31-39）、VPD の fail-closed 503、hosted-agent の「エージェント未設定」。

## 仕様参照
specs/00-architecture.md、specs/09-rag.md、specs/10 相当（NL2SQL）、specs/12 相当（voice）、
docs/tips.md

## 前提（依存タスク / 人間の事前作業）
- 依存: **FIX-47 が done**（genai.py fail-fast・`/api/rag/health` の器・jetuse:test の
  `jetuse-spike-fix47` スタックを再利用）。
- base ブランチ: **main**（`BASE_BRANCH=main`）。
- 人間ゲート: コミット / PR / push。**infra には触れない**（PORT-01 と衝突させない。
  TTS_REGION / METRICS_NAMESPACE の ORM 配線は PORT-01 側）。

## 対象 area
api（packages/api）。

## 作業内容
1. **モデル可用性の反映**: モデルごとの利用可否を判定（実装は軽量に: 初回失敗時に「利用不可」を
   プロセス内でマークする lazy 方式を基本とし、起動時プローブは任意）。
   `GET /api/chat/models` は利用可能なもののみ（または `available` フラグ付きで）返す。
   利用不可モデルへの chat は生エラーでなく「このリージョン/テナンシでは利用できません」を返す。
   既定モデルが利用不可なら chat-family（llama/gemini）へのフォールバックを応答に明示。
2. **dbchat の既定切替**: `semstore_ocid` が空なら既定バックエンドを select_ai にする。
   semantic store 明示指定で未設定なら「SemanticStore 未構成（SEMSTORE_OCID）。Select AI を
   使うか構成せよ」の設定ヒント付きエラー。
3. **Select AI の可視化**: bootstrap の ENABLE_RESOURCE_PRINCIPAL 失敗を起動ログで明確化し
   health（下記 8）に反映。プロファイル作成失敗のエラーに必要ポリシー（DG への
   generative-ai-family / バケット read）のヒントを付す。
4. **Speech / OCR / TTS の縮退メッセージ**: 未認可 404・サービス不在を機能別の明確な 503 に変換
   （TTS は「テナンシが us-phoenix-1 未購読の可能性」を明示。TTS_REGION 設定は尊重）。
5. **SH sample の検査**: bootstrap（または初回参照時）に SH 可視性を検査し、読めなければ
   sample ターゲットを disabled（UI に理由が出るエラー形）にする。
6. **obs の抑制**: log/metric ship 失敗の出力を指数バックオフ + N 回に 1 回のサマリへ。
   恒常的な 401/403 は WARNING 1 回で detach（以後リトライしない）を検討・実装。
7. **AUTH_MODE ガード**: `oci.config.from_file()` フォールバック 9 箇所を共通ヘルパ経由にし、
   設定ファイル不在時は「AUTH_MODE=resource_principal の設定漏れの可能性」を含む明確な
   設定エラー（未処理 500 にしない）。
8. **health の拡張**: FIX-47 の `/api/rag/health` を capability readiness へ拡張し、
   {chat(モデル別), rag, dbchat(semantic/select_ai), speech, ocr, tts} ごとに
   ok / degraded / unavailable + 理由を構造化して返す（Issue 報告者が自己診断できる粒度）。

## 完了条件（検証可能な述語で）
- `.venv/bin/pytest packages/api/tests` 全緑・`.venv/bin/ruff check packages/api` クリーン
  （新規分のユニットテスト: モデル可用性マーク・dbchat 既定切替・obs バックオフ・
  AUTH_MODE ガード・health 集約）。
- codex-review の review_verdict=PASS。
- 下記 E2E シナリオを jetuse:test 実環境で通過し、証跡を runs/<run-id>/e2e/ に記録。

## E2E シナリオ（完了ゲート・min_scenarios=2 以上。`jetuse-spike-fix47` スタックを再利用し、
修正版イメージは FIX-47 と同じ専用タグ方式で投入）
1. **モデル可用性**: `GET /api/chat/models` が実際に応答するモデルのみ（または available
   フラグ付き）を返し、利用不可モデル指定の chat がヒント付きエラーになること。
2. **dbchat 縮退**: SEMSTORE_OCID 未設定のクリーンルームで dbchat 既定が select_ai 経路で
   応答する（Select AI 側も不成立の環境なら、設定ヒント付きエラーになることの確認で代替し
   SKIPPED.md に理由を明記）。
3. **health 集約**: 拡張 health が機能別 readiness を返し、無効機能（例: semantic store 未構成）
   の理由が特定できること。
4. **obs 抑制**: コンテナログで ship 失敗 spam が抑制されている（意図的に権限を欠いた状態を
   作れる場合。不能なら単体テスト証跡 + SKIPPED.md）。
証跡: リクエスト/レスポンス・コンテナログ・CLI 出力。スタックの destroy はキュー後始末
（tasks/FIX47-PROGRESS.md）で行う。

## 成果物
- packages/api（models.py / chat.py / routes/chat.py / routes/dbchat.py / nl2sql.py /
  bootstrap.py / obs.py / voice 系 / tts.py / health + tests）
- docs/verification/port02-graceful-degradation.md（E2E 証跡サマリ）、実機の新発見は docs/tips.md
- Issue #47 返信コメント案への追記（自己診断手順 = 拡張 health の使い方。投稿は人間ゲート）

## 禁止事項
- 認証情報・テナンシ/コンパートメント OCID 実値・エンドポイント実値のコミット
- `jetuse-spike-` プレフィックス以外のリソース削除、jetuse:dev / jetuse:public の既存リソース変更
- OCIR `:latest` タグへの push、コミット / PR / push の無承認実行
- infra（infra/orm・modules）と loop-config.yml・スキル・hooks の編集

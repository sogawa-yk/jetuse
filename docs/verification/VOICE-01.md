# VOICE-01 検証レポート: 議事録生成（バッチ文字起こし+話者分離+LLM整形）

- 日付: 2026-06-12 / ブランチ: `task/voice-01` / 仕様: `specs/12-voice.md`
- 実行環境: devインスタンス上のローカルAPI（`~/.oci` ユーザー認証）+ Vite devサーバー（`/api` プロキシ新設）

## 実装

| 層 | 内容 |
|---|---|
| API | `/api/minutes`（一覧/アップロード+ジョブ作成/状態取得/削除）+ `/api/minutes/{id}/generate`（SSE整形） |
| コア | `jetuse_core/minutes.py`: Speech `WHISPER_MEDIUM`+diarizationジョブ、結果JSONの後処理（分かち書き結合・話者区間グルーピング）、テンプレート3種（議事録/FAQ/記事）のプロンプト |
| DB | migration `009_minutes.sql`（MINUTES_JOBS、CLOBにutterance配列JSON） — jetusedevへ適用済み |
| Web | `/minutes` ページ（アップロード+言語選択 / ジョブ一覧+状態バッジ+5秒ポーリング / 話者チップ+タイムスタンプ付きトランスクリプト / テンプレート選択+SSEストリーミング生成+コピー）。ナビの「議事録」を有効化 |
| 開発環境 | `vite.config.ts` に `/api`→`localhost:8000` の開発プロキシを新設（`VITE_API_PROXY` で上書き可） |

## E2E結果（ローカル・実OCIサービス）

1. **単一話者**（SPIKE-06のmeeting1.mp3）: アップロード→約10秒でSUCCEEDED→分かち書きが正しく結合された日本語テキスト（「本日の定例会議を始めます…」）→議事録生成成功
2. **2話者**（OCI TTS Phoenixで Yuki/Satoshi 交互4発話のWAVを合成して入力）:
   - `speaker_count=2`、4発話すべて正しい話者に分離。タイムスタンプも実時間と整合
   ```
   [  0.03-  6.91] 話者1: 本日の定例会議を始めます。まず先週の進捗を教えてください。
   [  7.75- 15.35] 話者2: バックエンドの愛実装は予定通り完了しました。来週から結合テストに入ります。
   [ 16.91- 22.91] 話者1: ありがとうございます。では結合テストの完了期限は今月末とします。
   [ 24.42- 29.36] 話者2: 了解しましたテスト環境の準備は私が担当します
   ```
   （「API→愛」はSTT側の聞き取りで、合成音声品質による）
3. **議事録生成**（gpt-oss-120b、SSE）: 出席者の役割推測 / 決定事項（期限・担当）/ 担当付きTODO表 / 時系列サマリを正しく抽出。創作なしの制約も遵守
4. **UI**: ブラウザ実機（Playwright）でアップロード〜生成まで操作確認。スクリーンショット `img/VOICE-01-minutes-ui.png`
5. ユニットテスト5件（時刻パース/話者グルーピング/ASCII境界スペース/プロンプト構築・打ち切り）追加 → API計69件pass

## 実機で確定したハマりどころ（docs/tips.mdにも追記）

- **TTS `synthesize_speech` は `compartment_id` 必須**（無いと404 NotAuthorizedOrNotFound — 認可エラーが404で返る）
- **MP3の単純連結（cat）はSTTが最初のストリームしか転写しない** → 検証音声はTTSの `output_format=PCM` で取得しWAVに結合するのが正解
- バッチSTT出力JSONの場所は `{出力prefix}/` 配下のジョブ依存パスのため、prefixでlist_objectsして特定する実装にした

## 実環境E2E（2026-06-12 完了）

- デプロイ: イメージ `0.19.0` + tfvars `SPEECH_BUCKET=jetuse-dev-speech` → terraform apply、SPA配信済み
- **IAMの実機切り分け**: `manage ai-service-speech-family` のみではRP作成ジョブが一律
  `INTERNAL_ERROR`(percent=0)でFAILED（ユーザー作成は成功 — `created-by` で切り分け）。
  公式要件どおりバケットレベル権限が必要で、`read buckets` + `read/inspect tag-namespaces`
  追加（人間作業）で解消
- IAM追加後: GW経由(M2Mトークン)で 2話者音声アップロード→**SUCCEEDED・話者分離2名とも正解**、
  FAQ生成SSEもGW経由で成功（1回、既知の間欠切断=backlog #12 が再現し再実行で成功）
- テストジョブはAPI経由のDELETEで削除済み（バケット側オブジェクトも消えることを兼ねて確認）

## 残課題（specs/12の範囲外）

- 長時間音声（>30分）の実測は未実施（打ち切り注記の動作はユニットテストで担保）
- VOICE-02（リアルタイム）/ VOICE-03（音声チャット）は未着手

## 追補（2026-06-12 ユーザーフィードバック対応）: 一覧バッジが「処理中」のまま残る問題

- 原因: 状態同期が詳細取得（`GET /api/minutes/{id}`）にしかなく、一覧はDBの古いstatusを返していた
  （フロントの5秒ポーリングでも、詳細と一覧の発行順の競合で最終状態を取りこぼす）
- 修正（イメージ0.19.1）: ①一覧APIでもprocessing行（上限5件）をOCIジョブへ同期 ②フロントは詳細レスポンスで一覧バッジを即時更新
- 実環境検証: アップロード→**一覧APIのみのポーリングで10秒後にcompletedへ遷移**することを確認

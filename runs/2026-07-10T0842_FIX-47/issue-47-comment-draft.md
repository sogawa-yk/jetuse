# Issue #47 返信コメント案（投稿は人間ゲート — 投稿前に E2E 完了で内容確定）

---

ご報告ありがとうございます。原因を特定し、修正を進めています。

## 原因

JetUse の RAG・チャット既定モデル・会話メモリが使う OCI GenAI の状態 API（Files / Vector Store
files / Conversations / Responses）は、`OpenAi-Project` ヘッダに **GenerativeAiProject の OCID**
を要求します（公式ドキュメント未記載の仕様）。現行の公開スタックはこの OCID を配線しておらず、
開発環境に手動作成された project に暗黙依存していました。このため **GenerativeAiProject が無い
テナンシ/コンパートメントでは RAG アップロードが必ず失敗**します（さらに既定チャットモデルと
会話の文脈保持にも同根の影響があります）。

404 が `vector_stores.create`（CP）で出ている場合は、リソースプリンシパルの IAM 権限も併せて
確認が必要です（下記 3.）。

## 修正（次リリースに含まれます）

1. **PROJECT_OCID の自動解決**: スタック変数 `project_ocid` を新設(任意)。未指定なら
   アプリがコンパートメント内の ACTIVE な GenerativeAiProject を自動検出し、無ければ
   `jetuse-project` を自動作成して使います(スタック変数 `enable_project_autocreate`、既定オン。
   オフにする場合は `project_ocid` の明示指定を推奨)。
2. **IAM policy の追加**: 自動作成に必要な `manage generative-ai-project` を runtime policy に
   追加しました(`enable_project_autocreate` と連動)。
3. **自己診断エンドポイント**: `GET /api/rag/health` が ①project 解決 ②コントロールプレーン
   ③データプレーン（OpenAi-Project 付き）の 3 点を検査し、どこで失敗しているかをヒント付きで
   返します。RAG 系のエラーも 500 ではなく原因ヒント付きの 503/502 になります。

## お手元での確認方法（修正版デプロイ後）

1. `GET <app_url>/api/rag/health` を実行 → `checks.project / control_plane / data_plane` の
   どこが `ok: false` かを確認。
2. `project` が false → スタック変数 `project_ocid` の設定、または runtime policy に
   `manage generative-ai-project` があるか確認。
3. `control_plane` / `data_plane` が false → 次を確認:
   - Dynamic Group の matching rule に `computecontainerinstance`（と `fnfunc`）が含まれ、
     `resource.compartment.id` がデプロイ先コンパートメントを指していること
   - runtime policy の `use generative-ai-family` / `manage generative-ai-vector-store` /
     `manage generative-ai-vectorstore-file` / `manage generative-ai-file` /
     `manage generative-ai-project` が同コンパートメントに付与されていること
   - デプロイリージョンが OpenAI 互換 agentic API に対応していること

## クリーンルーム検証結果（2026-07-13 実施）

GenerativeAiProject が 1 つも無いクリーンルームのコンパートメントに公開スタックをデプロイし:

- **再現**: 現行公開イメージで `POST /api/rag/files` → 500 を確認（実体は状態系 API が
  project 欠如で 4xx → 未処理）。会話の文脈保持も同根で失敗することを確認。
- **修正版**: PROJECT_OCID 未指定でアップロード成功（project 自動作成 → 索引化 →
  file_search の出典付き応答まで一気通貫）。明示指定時は自動作成が発動しないこと、
  無効値を入れた場合は 500 でなくヒント付き 503 + `/api/rag/health` での失敗点特定も確認済み。
- **IAM**: 修正版の runtime policy 一式（`manage generative-ai-project` 含む）だけで全経路が
  動作することをリソースプリンシパルで実証しました。

### 補足: 初回リクエストについて

project の自動作成直後、その project がデータプレーンへ伝播するまで**数分**かかることがあります。
その間 RAG アップロードは一時的に 503（ヒント付き）を返します。`GET /api/rag/health` の
`ok` が `true` になってから再試行してください。

## 追記（PORT-02, 2026-07-13）: 自己診断が RAG 以外にも拡大しました

`GET /api/rag/health` に加えて、**`GET /api/health`** で RAG 以外の機能（チャットモデル可用性・
dbchat・音声・OCR・TTS）もまとめて自己診断できるようになりました。

- `capabilities.chat.models` — モデルごとに `ok`。リージョン/テナンシで提供されていない
  モデルへのチャットは、以前は生のプロバイダエラーでしたが、今は「このリージョン/テナンシでは
  利用できません」というヒント付きメッセージになり、**既定モデルが使えない場合は自動的に
  別モデルへフォールバック**します（応答に自動フォールバックした旨を明示）。
- `capabilities.dbchat` — `SEMSTORE_OCID` が未設定の環境（本 Issue と同根の「別テナンシで
  未整備な機能」パターン）では、dbchat の既定質問（サンプルデータへの質問）が失敗する代わりに
  自動的に Select AI 経路へ切り替わるようになりました。`select_ai` の `ok` が `null` の場合は
  「起動時のクレデンシャル検証が未実行/未確認」という意味で、`false`（確実に失敗）とは区別
  されます。
- `capabilities.speech` / `capabilities.ocr` / `capabilities.tts` — 必要な設定
  （`SPEECH_BUCKET`・`COMPARTMENT_OCID`）が空の場合に `unavailable` として明示します。
  TTS は Phoenix リージョン限定のサービス制約があり、テナンシがそのリージョンを未購読の場合は
  「テナンシが us-phoenix-1 未購読の可能性」というヒント付き 503 になります。

いずれも実環境（jetuse-spike-47 スタック）で実際の応答を確認済みです。原因不明な機能不調を
報告いただく際は、まず `GET /api/health` の結果を添えていただけると切り分けが早くなります。

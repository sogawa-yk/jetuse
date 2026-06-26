# SBA-02 検証レポート — AI組込フレームワーク + コア同梱 sample-app SBA-A（サポートデスク業務アプリ / RAG）

**タスク**: SBA-02 / **ブランチ**: feat/SBA-02 / **日付**: 2026-06-26
**仕様参照**: docs/enhance/202607-demo-platform-plan.md §6（SBA-A）/ specs/16-platform.md / 依存: SBA-01, PLG-07

## 1. 何を作ったか

「業務アプリ＋AI」を実証し、以降のサンプルアプリの型（AI 組込スロットの**実行時バインド**）を確立した。

- **AI 組込スロット実行時バインド機構** `jetuse_core/plugins/ai_runtime.py`
  - `capability → handler` レジストリ（`@register_capability`）。aiSlot は capability を宣言するだけで、
    実行時にハンドラへ束縛する（`bind_slot` / `invoke_slot`）。未束縛能力は `UnboundCapabilityError`。
  - SBA-02 で束縛した能力: `rag.search`（FAQ-RAG 回答）/ `classify`（自動分類）/ `summarize`（要約）/
    `draft`（返信ドラフト）。`nl2sql`/`chart`/`agent`/`minutes`/`vlm.ocr` は SBA-03..05 で束縛（`unbound_capabilities` で点検可能）。
  - 知識コーパスは文脈（`SlotContext.corpus`）で渡す＝**業務アプリ自身のデータ（FAQ シード）に AI を組み込む型**。
    検索は外部ベクトルストア非依存の軽量スコア（ASCII 単語トークン＋CJK バイグラム）で実環境では GenAI 推論のみで安定。
- **コア同梱 sample-app SBA-A「サポートデスク(問い合わせ管理)」** `jetuse_core/plugins/sample_app_builtin.py`
  - `kind: sample-app` の manifest として定義（`validate_sample_app` / `validate_composition` を満たす）。
  - データモデル: `faqs`（FAQ ナレッジ 10件＝RAG/draft の知識源。question/answer/category/views/updated_at）/
    `inquiries`（問い合わせ 7件。id/subject/customer/body/thread/category/priority/status/received_at）。
  - aiSlots: `faq-answer`(rag.search) / `auto-classify`(classify) / `summarize-thread`(summarize) / `reply-draft`(draft)。
- **API ルート** `service/routes/sample_apps.py`: 一覧 / 定義取得 / `slots/{key}/invoke`（実行時バインド実行・JSON 応答）。
- **Web** `pages/sampleapp.tsx` = **サポートデスク業務アプリ**（既存デザインシステム流用）:
  - 受信トレイ（DataTable＋StatusBadge：状態/件名/顧客/カテゴリ/優先度/受信日時、検索＋状態/カテゴリフィルタ、新規/対応中/解決の件数サマリ）
  - 問い合わせ詳細（業務UIにAI埋込）：顧客情報・**会話スレッド(チャット吹き出しUI: 顧客=左/サポート担当=右の色分け吹き出し＋発言者名・時刻。本文も最初の発言として一連で表示)**・ステータスライフサイクル・担当者割当に加え、
    自動トリアージ(classify×2→採用)・ナレッジ提案(rag.search→返信に使う)・返信ドラフト(draft→コピー/送信デモ=スレッドへ agent メッセージとして構造化追記)・スレッド要約(summarize)
  - スレッドは seed で発言者ロール付き構造化メッセージ（JSON）として保持し、UI は JSON/旧文字列の両方をパースする（後方互換）。
  - ナレッジ(FAQ DataTable：質問/カテゴリ/参照数/更新日＋検索。RAGがこのKBを引く旨を明示)
  - `pages/home.tsx`（home 実行導線「業務アプリデモ」カード）/ ルート `/sba/:id`。AIは提案バッジ・引用元参照・採用ボタン・grounded表示で「効いている感」を出す。

## 2. テスト・lint

| 項目 | 結果 |
|---|---|
| `.venv/bin/pytest packages/api/tests` | **415 passed**（ai_runtime / sample_app_builtin / route + 入力ガード）/ coverage 66% |
| `ruff check packages/api` | clean |
| `npm --prefix packages/web run test`（vitest） | **76 passed**（サポートデスク主要フロー: 受信トレイ/トリアージ採用/低信頼注記/会話スレッド(チャットUI)描画/旧文字列スレッド後方互換/返信送信の構造化追記/要約/ナレッジ/HTTPステータス + home耐性3件＋既存） |
| `npm --prefix packages/web run lint`（eslint） | clean |
| `npm --prefix packages/web run build` | 成功（tsc + vite） |

## 3. 実環境 E2E（jetuse-dev / 実 OCI GenAI ap-osaka-1）

実プロセスの API（`uvicorn service.main:app`）を実 GenAI に接続し、実 HTTP で検証。証跡は
`runs/2026-06-26T0252_SBA-02/e2e/`（deploy.log / scenario-*.json / README.md / SKIPPED.md）。
**Web UI と同一に `model` フィールドを送らない既定実行経路**で実施（既定モデル `settings.sample_app_model`
= `llama-3.3-70b`。project_ocid 不要で追加設定なしにデモが動く。`SAMPLE_APP_MODEL` で上書き可）。

業務フロー(受信トレイ→詳細→トリアージ→ナレッジ→返信→要約)の各 AI 組込点を実機実行（詳細は e2e/README.md）:
- **起動**: `GET /api/sample-apps`/定義 で SBA-A サポートデスクが起動（inquiries 7件・faqs 10件）。
- **自動トリアージ(classify×2)**: 「ログイン不可・ロック」→ category=**アカウント**／priority=**高**。
- **ナレッジ提案RAG(rag.search)**: 同入力 → grounded=True、「15分待つと自動解除/管理者へ依頼」をロックFAQ根拠に回答＋引用元提示。
- **返信ドラフト(draft)**: 宛名〜結びの返信文を生成。 **スレッド要約(summarize)**: API レート制限の会話を3行に要約。
- **多層防御**: 無関係入力 → 関連度ゲート(MIN_RAG_RELEVANCE)で grounded=False・引用なし。grounded LLM も推測を拒否（二重）。

**ブラウザ E2E（実 google-chrome / 配信済み実ビルド成果物）**: API/GenAI シナリオに加え、配信済み SPA（実 `dist`）を
実 chrome(149 headless / CDP)で操作する E2E を実施（証跡 `runs/.../e2e/browser/`、`assertions.json`=**17/17 PASS**、
スクショ 8枚）。home 導線→受信トレイ→詳細（会話スレッドのチャットUI）→AIトリアージ採用→返信ドラフト→送信→ナレッジを**実クリックで遷移・描画・AI起動**まで確認
（routing/認証ヘッダ付き fetch/実ビルド成果物/ブラウザ描画/詳細→AI操作）。フロント描画は mock API（canned）で、AI 出力の
実環境妥当性は下記 scenario-* の実 GenAI で、二層担保。

**未実施範囲（理由は SKIPPED.md）**: ① フルコンテナ/APIGW 配信での最終デモ品質目視（人間ゲート: terraform apply 承認・目視デモ品質）。
② ADB スキーマ JETUSE_SBA02 隔離（本機能は ADB ステートレス＝コア同梱定義を直接コーパス化。scaffold/隔離は SBA-01 責務で検証済。
共有 loop ADB の ADMIN 資格情報失効・並行タスク影響回避）。③ オプションの gpt-oss-120b 実呼び出し（既定経路ではない。
既定は project 不要な llama-3.3-70b に変更済で Web UI 既定経路を実証。gpt-oss-120b は project_ocid＋`SAMPLE_APP_MODEL` で deployed env にて利用可）。

## 4. 人間チェックポイント（デモ品質）

- 実環境の RAG 回答品質（S1/S2）は FAQ と整合し良好。無関係入力（S5）は関連度ゲートで grounded=False。
  実運用ではベクトル検索/File Search への束縛差し替え（runtime binding の利点）で更なる精度改善余地あり。
- フルコンテナ + API Gateway デプロイとブラウザでのデモ品質確認は**人間ゲート**として残る。
- **入力ガード統合（実装済み）**: slot invoke ルートに chat/usecase と同じ入力ガードを結線した
  ——モデレーション（`MODERATION_ENABLED`）/プロンプトインジェクション検知（`PROMPT_INJECTION_GUARD_ENABLED`）と
  監査ログ（`audit.py`）。いずれも**既定 OFF**で、フラグ ON 時にフラグ入力を 400 でブロックし監査イベントを記録する
  （単体テスト `test_invoke_moderation_block_when_enabled`）。既定（OFF）のデモ経路は不変。
  ガード本体は SEC-02/GAP-01 で実装済みの共通モジュールをそのまま通す（本タスクはルート結線）。
  公開運用でのフラグ常時 ON 化と実 OCI ガードサービスの実呼び出し E2E はフラグ運用方針＝人間ゲート（e2e/SKIPPED.md §5）。

## 5. 設計判断メモ

- `rag.search` を「コア同梱 FAQ シードを知識源とする軽量 RAG」に束縛した。実環境で GenAI 推論のみで安定動作し、
  外部ベクトルストアの伝播待ち/コストに依存しない。
- 軽量検索は overlap 係数で関連度を測り、無関係入力は grounded=False に、引用は最上位の関連度に対する相対比
  (RAG_CITATION_TOP_FRACTION)で絞る。さらに丁寧表現・接続辞のバイグラム（「ください」等）を stoplist で
  特徴から除外し、内容語一致に集中させる。結果: S1/S2 の引用は各 1 件、S4(API 質問)は API FAQ を正しく根拠に、
  S5 無関係入力は引用ゼロ（grounded=False）。
- **更なる精度（曖昧言い換え・同義語）** は File Search（per-instance Vector Store）への束縛差し替えで対応する。
  runtime binding の差し替え点として将来対応（SKIPPED.md ②）。本タスクの軽量実装は実環境 GenAI のみで安定動作する。

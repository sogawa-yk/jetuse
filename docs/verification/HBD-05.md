# HBD-05 構成サマリ出力（顧客提示用）＋一気通貫 E2E 検証レポート

- タスク: HBD-05 構成サマリ生成＋一気通貫（ヒアリング→Q&A→合成→バリデーション→プレビュー→デモ起動）
- area: both（api：サマリ生成＋デモ起動 / web：一気通貫導線＋サマリ画面＋エクスポート）
- base: feat/stage-2（HBD-01..04 統合済）
- run: `runs/2026-06-26T1956_HBD-05/`
- 仕様参照: `docs/enhance/202607-demo-platform-plan.md` §5.1 / §9 / §10「HBD-05」、
  `docs/enhance/202607-hearing-flow.md` §5（推薦構成サマリ）

## 1. 何を作ったか

HBD-01..04 を一気通貫で接続し、合成・検証済みのデモ構成から **顧客提示用の構成サマリ** を生成し、
「ヒアリング確定 → 合成 → バリデーション PASS → プレビュー → デモ起動」を成立させた（S1+S2 体験の出口）。

### api
- `jetuse_core/summary.py`（新規）: `build_summary(composition, *, narrative=None) -> DemoSummary`。
  hearing-flow §5 の4項目を出力する。
  - **①構成図**（どのデータに何の AI が効くか）: `composition.screens` の active 組込点から決定的に導出。
  - **②使う OCI サービス**（固定リファレンス基盤の該当部分）: capability/connector → OCI サービスの
    決定的写像（`CAPABILITY_OCI_SERVICES` / `CONNECTOR_OCI_SERVICES` / ADB 基盤）。
  - **③デモ手順**: 画面・active 組込点・コネクタから決定的に組み立て。
  - **④想定効果**: GenAI 文章化（§6 ④）。不在/失敗は決定的テンプレへフォールバック（`impact_source` で出所明示）。
  - **エクスポート**: `summary_to_markdown()` でプリセールス転用の Markdown を同梱。
  - 設計: ①〜③は合成結果から**決定的に**導出（捏造しない）。④の文章化のみ GenAI 補助。`build_summary` は
    副作用なしの純関数で、GenAI 文章化は `hearing_genai.summary_narrative()`（フォールバック付き）に分離。
- `jetuse_core/hearing.py`: `record_launch` / `get_launch`（デモ起動記録の永続。upsert・所有者境界付き）。
- `jetuse_core/migrations/018_demo_launch.sql`（新規）: `demo_launch` テーブル（1セッション1起動・冪等再適用）。
- `service/routes/hearing.py`:
  - `POST /api/hearing/sessions/{sid}/launch`: 確定済み推薦→合成→**ガバナンス4制約 PASS をデプロイ前ゲート**に
    起動記録。FAIL は **409＋機械可読な違反＋代替提案**（外れたデモを起動させない＝境界）。
  - `GET  /api/hearing/sessions/{sid}/launch`: 起動済みデモ記録。
  - `POST /api/hearing/sessions/{sid}/summary`: 構成サマリ（④は実 GenAI 文章化、失敗時フォールバック）。
  - `GET  /api/hearing/sessions/{sid}/summary/export`: Markdown（text/markdown 添付）。再現可能化のため
    エクスポートの④は決定的テンプレに固定。
  - **summary/export はガバナンス PASS をゲートにする**（`_governance_gate`）。起動できない（ガバナンス FAIL）
    構成では顧客提示サマリも生成させない（「起動済みデモのサマリ」の整合。FAIL は 409＋代替提案）。
  - **起動記録の陳腐化対策**: 回答変更（`save_answer`）・再推薦（`save_recommendation`）で推薦が陳腐化
    する経路で `demo_launch` も削除する（GET /launch が陳腐な構成を返さない）。
- テスト: `tests/test_summary.py`（6 ケース・決定的導出/フォールバック/Markdown）、
  `tests/test_hearing_route.py`（launch/summary/export 11 ケース追加）。

### web
- `pages/preview.tsx`: 一気通貫の出口を集約。`CompositionPreview`（HBD-03）に加え、利用者操作で
  **検証(ValidationPanel) → 起動(LaunchPanel) → サマリ(SummaryPanel)** を順に進める。検証 PASS のときだけ
  「このデモを起動」可（FAIL は起動不可＋代替提案へ誘導）。**Markdown エクスポートはサーバの正準
  `GET /summary/export` を取得**してダウンロードする（API を唯一の出力源にし、画面/ドキュメントと一致）。
- `pages/hearing.tsx`: 推薦確定後に `/preview/{sid}` への導線（一気通貫の接続）。
- i18n: `hearing.result.toPreview`（ja/en）。
- テスト: `pages/preview.flow.test.tsx`（7 ケース・検証/起動/サマリ/境界）。既存 `preview.ui.test.tsx` も維持。

## 2. 受け入れ条件の充足

- [x] 構成サマリ生成（①構成図 ②使う OCI サービス ③デモ手順 ④想定効果）を出力する
- [x] サマリ文章化は GenAI 補助だが、構成図・使用サービスは合成結果から**決定的に**導出（捏造しない）
- [x] 一気通貫導線: 確定→合成→バリデーション PASS→プレビュー→「起動」で実 loop 環境にデモが立ち上がる
- [x] サマリは Markdown でエクスポート可能（プリセールス転用）
- [x] web build・vitest・eslint / api lint（ruff）クリーン。サマリ生成と一気通貫の結合テストを追加

## 3. 単体・結合テスト / lint / build

```
api:  .venv/bin/pytest packages/api/tests   → 612 passed（cov 70.8% ≥ 45%）
api:  .venv/bin/ruff check packages/api      → All checks passed!
web:  npm run lint                           → clean
web:  npm run test (vitest)                  → 106 passed（16 files）
web:  npm run build                          → built（既存の chunk-size 警告のみ）
```

## 4. 実環境 E2E（jetuse-dev / loop ADB・専用スキーマ JETUSE_HBD05 隔離）

証跡: `runs/2026-06-26T1956_HBD-05/e2e/`（`deploy.log` / `e2e-run.log` / `scenario-1.json` /
`scenario-2-summary.json` / `scenario-2-summary-export.md` / `scenario-3.json` / `SKIPPED.md`）。

- デプロイ: loop ADB `jetuse-loop-adb`（AVAILABLE）再利用 → 専用スキーマ `JETUSE_HBD05` 隔離 → migrate
  （001..018 全適用＋2回目 no-op の冪等再適用クリーン）。ウォレット/パスワードは外部 scratchpad のみ。
- **シナリオ1（一気通貫・正常＋主役 AI 実機）**: 確定→合成→`validate`(governance.ok=true・全制約 PASS)→
  `launch`(demo_launch を JETUSE_HBD05 に永続)→ 起動デモの主役スロット `faq-answer`(rag.search) を
  **実 GenAI で invoke** し、実応答（パスワード再設定の案内）を取得＝主役 AI 機能が実際に動くことを実証。
- **シナリオ2（顧客提示サマリ）**: `summary`（impact_source=genai＝実 GenAI 文章化、構成図4経路/OCIサービス
  5件/手順7）＋ `summary/export`（text/markdown）を取得・保存。
- **シナリオ3（境界）**: SBA-A＋業務DB で nl2sql が許可外組合せ → `validate` governance.ok=false →
  `launch` が **409**（代替提案: 主アプリを SBA-B/SBA-C へ）→ `GET launch` 404（起動記録なし）。
- 結果: **ALL PASS**。限定/代替範囲は `e2e/SKIPPED.md` に明記（ブラウザ実機は vitest＋build で代替、
  「起動」は loop 基盤上のデモ＝S4 のコンテナ配備は非ゴール）。

## 4.1 Codex レビュー（証跡込み）

- review-3 = **PASS**（blocker 0 / major 2 / minor 1、E2E adequacy: sufficient・4 シナリオ）。
- 対応: ④決定的フォールバックを **active な部品の効果のみ**から合成するよう修正（未組込機能を顧客提示文に
  書かない＝捏造しない / review-3 F-001）。再検証時に古い起動/サマリ表示をクリア（F-003）。
- **既知の残課題（review-3 F-002・本プロトタイプでは許容）**: `launch` は「推薦読取→合成→`record_launch`」が
  同一トランザクションでないため、起動中に別クライアントが回答変更/再推薦すると、無効化後に旧構成で
  起動記録が再作成される極小ウィンドウのレースが理論上残る。通常の単一 SA フローでは発生せず、次の
  回答変更/再推薦で自己修復する（`demo_launch` 削除）。厳密化（推薦状態の `SELECT ... FOR UPDATE` 直列化）は
  ガバナンス本実装（S3 のトークン実行時強制）と併せた別タスクとする。

## 5. 残る人間ゲート

- コミット / PR / push（feat/HBD-05 → base feat/stage-2）。
- 一気通貫デモの品質確認（タスクの人間ゲート: デモ品質）。
- E2E のため loop ADB「jetuse-loop-adb」の ADMIN パスワードを再設定した。承認根拠は
  `runs/2026-06-26T1956_HBD-05/e2e/APPROVAL.md`（jetuse-loop-adb は参照のみの既存リソースではなく
  jetuse-dev の loop E2E 専用リソース＝変更はユーザー承認済み 2026-06-25＋memory の確立パターン）。
  既存リソース（VCN develop / インスタンス dev / バケット）は参照のみ・変更なし。新規実リソース作成なし。

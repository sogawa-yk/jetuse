# HBD-03 検証レポート — 合成エンジン＋プレビュー

- **run_id**: 2026-06-26T1821_HBD-03
- **area**: both（api: 合成エンジン＋プレビュー定義生成 / web: プレビュー表示）
- **base**: feat/stage-2（HBD-01 推薦＋SBA-01/02 レジストリ/ai_runtime を素材に合成）
- **実環境**: jetuse-dev / loop ADB `jetuse-loop`（db_name `jetuseloop`, AVAILABLE）を**再利用**。
  DB は専用スキーマ `JETUSE_HBD03` で隔離（Oracle 識別子はハイフン不可のため `JETUSE_HBD03` に正規化）。
- **証跡**: `runs/2026-06-26T1821_HBD-03/e2e/`（deploy.log / scenario-1..3.json / SKIPPED.md）

## 1. 成果物

| 種別 | パス |
|---|---|
| 合成エンジン＋プレビュー定義 | `packages/api/jetuse_core/synth.py`（`synthesize()` → `DemoComposition`） |
| プレビュー API | `packages/api/service/routes/hearing.py`（`POST /api/hearing/sessions/{sid}/preview`） |
| web プレビュー画面 | `packages/web/src/pages/preview.tsx`（`CompositionPreview` ＋ ルート `/preview/:sid`） |
| 合成の単体テスト | `packages/api/tests/test_synth.py`（12）/ `test_hearing_route.py`（preview 3 追加） |
| web UI テスト | `packages/web/src/pages/preview.ui.test.tsx`（3） |
| 検証レポート | 本ファイル |

## 2. 設計の要点

- `synthesize(Recommendation)` は **副作用なしの決定的関数**（DB/GenAI 非依存）。HBD-01 の推薦を入力に、
  `sample_app_registry` のコア同梱 SBA 定義へ、推薦 AI 部品を `ai_runtime` の束縛レジストリ
  （`bound_capabilities()`）で**実行時バインド**したデモ構成 `DemoComposition` を生成する。
- **未束縛/組込点なしの部品は active から外し、理由を `excluded`/`warnings` に残す**（黙って消さない）。
- **シード方針（Q6）を構成へ反映**: `sample`=コア同梱シードを投入（行数>0）/ `genai_generated`=生成は取込時の
  別ターンのためプレビュー時点は投入予定 0 行（構造のみ）/ `replace_later`=投入 0 行。
- **配布表現（再検証可能）を壊さない**: 元の検証済み定義は変形せず、`validate_composition` の
  `CompositionReport`（必要ケイパ/権限スコープ整合）を同梱。
- 境界は **安全に失敗**: 主SBA 未確定（Q1=other）/未実装 SBA（SBA-D）は `ok=false`＋`errors` の
  描画可能な失敗構成を返す（`strict=True` で `SynthesisError`）。HBD-04 の前段チェックに渡せる形。

## 3. 静的検証

- `.venv/bin/pytest packages/api/tests`: **576 passed**（synth 12＋hearing route preview 3 を含む）。
- `.venv/bin/ruff check packages/api`: **All checks passed**。
- web: `npm run build` 成功 / `npm run lint`（eslint）クリーン / `npm run test`（vitest）**87 passed**（preview 3）。
- 既存公開シグネチャ非破壊（route 追加のみ。新規 migration なし＝HBD-01 の hearing テーブルを再利用）。

## 4. デプロイ（完了ゲート）

`deploy.log`:
- loop ADB を再利用し、ADMIN パスワード／ウォレットを都度再生成、専用スキーマ `JETUSE_HBD03` を隔離作成。
- `python -m jetuse_core.migrate` で 001..017 を適用、**冪等再適用＝`(none — up to date)`** を確認。

## 5. 実環境 E2E（3 シナリオ・全 PASS / 計 28 checks）

| # | シナリオ | 結果 | 主な確認 |
|---|---|---|---|
| 1 | SBA-A 推薦→合成→プレビュー（構成通り） | **PASS** | recommend=SBA-A を実 ADB に永続。preview 200／`ok=true`／active に {rag.search,summarize,classify}／画面 faq/inbox/console 描画／console に RAG 組込点／UI=chat・connector=slack／seed=sample 投入行>0／composition_report 再検証 OK |
| 2 | NL2SQL（SBA-B）実 capability バインド | **PASS** | recommend=SBA-B。`nl2sql`/`chart` が active／binding=active／query 画面に nl2sql 組込点が現れる |
| 3 | 境界（安全に失敗/警告） | **PASS** | 3a: vlm.ocr（未束縛・SBA-A に組込点なし）は active 除外＋excluded＋warnings、genai_generated は投入 0 行。3b: Q1=other → preview `ok=false`＋errors＋screens 空。3c: 推薦なし→409。3d: 未知セッション→404 |

各シナリオの HTTP 応答・DB 状態・チェック内訳は `scenario-<n>.json`。未実施範囲（ブラウザ実描画・GenAI 最近傍）は
`SKIPPED.md` に理由明記。

## 6. 受け入れ条件の充足

- [x] 合成エンジン: 推薦→`sample_app` 定義へ AI スロットを実行時バインドしたデモ構成オブジェクトを生成
- [x] AI 部品は `ai_runtime` の capability レジストリから束縛、未束縛は構成（active）に含めず理由を残す
- [x] シード方針（sample/genai/replace_later）を構成へ反映
- [x] プレビュー: 画面・組込点・使う AI・データを実行せず描画、配布表現（composition_report）を壊さない
- [x] api lint（ruff）/ web build・vitest・eslint クリーン、合成の単体テスト（代表＋未束縛/不整合）を追加

## 7. 残る人間ゲート / 非ゴール

- **人間ゲート**: コミット / PR / push（未実施）。IAM・テナンシ・既存リソース変更なし。
- **非ゴール**: 厳密な合成バリデーション（許可組合せ・必要ケイパ網羅）= HBD-04。実デプロイ（コンテナ配備）= S4。
  本タスクは構成生成＋描画＋前段の整合チェックまで。

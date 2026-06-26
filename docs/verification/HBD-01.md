# HBD-01 検証レポート — ヒアリングフロー＆推薦ルールエンジン

- **run_id**: 2026-06-26T1700_HBD-01
- **area**: both（api 中心。web は本タスクでは疎通最小=非対象）
- **base**: feat/stage-2
- **実環境**: jetuse-dev / loop ADB `jetuse-loop-adb`（dbname `jetuseloop`、AVAILABLE）を**再利用**。
  DB は専用スキーマで隔離。
- **証跡**: `runs/2026-06-26T1700_HBD-01/e2e/`（deploy.log / scenario-1..3.json）

## 1. 成果物

| 種別 | パス |
|---|---|
| migration | `packages/api/jetuse_core/migrations/017_hearing.sql`（hearing_session / hearing_answer / recommendation） |
| 質問スキーマ | `packages/api/jetuse_core/hearing_schema.py`（Q1..Q6＋Auto・検証） |
| 推薦エンジン（決定的） | `packages/api/jetuse_core/recommend.py` |
| GenAI 補助（§6 境界） | `packages/api/jetuse_core/hearing_genai.py`（①メモ要点抽出 ②最近傍SBA） |
| 永続リポジトリ | `packages/api/jetuse_core/hearing.py` |
| API ルート | `packages/api/service/routes/hearing.py`（`/api/hearing/*`） |
| 仕様昇格 | `specs/16-platform.md` §11（hearing-flow.md を昇格） |
| migrate ランナー（冪等強化） | `packages/api/jetuse_core/migrate.py`（DDL 重複の冪等スキップ） |

## 2. 静的検証

- `.venv/bin/pytest packages/api/tests`: **565 passed**（hearing 系: schema/recommend/route/genai/migrate）。
- `.venv/bin/ruff check packages/api`: **All checks passed**。
- 既存公開シグネチャ非破壊（route 追加のみ。`service/main.py` に `hearing` router を include）。

## 3. デプロイ（完了ゲート）

`deploy.log`:
- loop ADB を再利用し、専用スキーマ `JETUSE_HBD01` を作成（ADMIN パスワード／ウォレットは都度再生成）。
- `python -m jetuse_core.migrate` で 001..017 を適用（**017_hearing 含む**）。
- **冪等再適用**: 2 回目の migrate は `applied: (none — up to date)`。3 テーブル＋2 一意制約の存在を確認。

> スキーマ名: タスクの `JETUSE_HBD-01` は Oracle 識別子にハイフンを含められないため、有効識別子
> `JETUSE_HBD01` に正規化（隔離の意図＝HBD-01 専用スキーマは保持）。

## 4. 実環境 E2E（3 シナリオ・全 PASS）

| # | シナリオ | 結果 | 主な確認 |
|---|---|---|---|
| 1 | 正常系（決定ルール＋永続） | **PASS** | support＋docs＋rag_qa → SBA-A ＋{rag.search,summarize,classify}＋slack＋chat＋sample。実 ADB の `recommendation`/`hearing_answer`(6件) 永続、confirm で `confirmed_at` セット |
| 2 | GenAI 補助（実 ap-osaka-1） | **PASS** | コールセンターのヒアリングメモ貼付 → 実 GenAI(chat/completions, llama-3.3-70b)が Q1..Q6 を抽出 → **6 件すべて `source=genai_suggested` で実 ADB 永続** |
| 3 | フォールバック/境界 | **PASS** | (3a) GenAI 非依存で決定ルールのみ推薦成立（inventory→SBA-B/nl2sql）。(3b) Q1=other → `sample_app=null`＋`needs_genai_nearest=true` を永続、GenAI 助言 `genai_nearest_sample_app=SBA-B` を添付。(3c) 不正選択肢=422 / 不完全回答=422 / 未知セッション=404 |

各シナリオの HTTP 応答・DB 状態・チェック内訳は `scenario-<n>.json` に記録。

## 5. 受け入れ条件の充足

- [x] migration で 3 テーブルを追加し冪等再適用が成功（deploy.log）
- [x] 質問スキーマ（Q1..Q6＋Auto）をコードで定義しスキーマ検証テストが通る
- [x] 推薦ルールエンジンを決定的関数として実装、代表ケース網羅テストがパス（例: サポート＋文書＋RAG-QA→SBA-A＋{RAG-QA,要約,分類}）
- [x] GenAI 補助は §6 の境界に限定（①②実装。③④はサマリ文章化=HBD-03 側へ）。GenAI 不在/失敗でも決定ルールで推薦が成立（フォールバックを単体＋E2E 3a で確認）
- [x] API: セッション CRUD＋回答保存＋`POST recommend`（決定的＋任意で GenAI 助言）。既存シグネチャ非破壊
- [x] api lint（ruff）・型・既存テスト後方互換クリーン

## 6. 残る人間ゲート / 非ゴール

- **人間ゲート**: コミット / PR / push（未実施）。IAM・テナンシ・既存リソース変更なし。
- **非ゴール**: ダイアログ UI=HBD-02 / 合成=HBD-03 / 本格バリデーション=HBD-04。複合（主＋従SBA）は
  MVP では単一に絞り `secondary_sample_apps` を空で保持（§8 未決）。GenAI ③シード生成・④サマリ文章化は
  保存素地のみ用意（生成本体は後段）。

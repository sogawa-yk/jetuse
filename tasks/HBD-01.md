# タスク: HBD-01 ヒアリングフロー＆質問スキーマ＋推薦ルールエンジン

## ゴール
顧客ヒアリングを構造化保存し、回答から「主サンプルアプリ＋AI部品＋コネクタ」の推薦構成を
**決定的ルール（監査可能）＋GenAI補助**で生成する基盤（データモデル＋質問スキーマ＋推薦API）を用意する。

## 対象 area
both（api 中心：データモデル＋質問スキーマ＋推薦エンジン＋API。web は最小の疎通のみ）

## 依存
ステージ1（SBA-01/02 で sample-app の型と AI 組込点が確定済み）。S2 の先頭タスク。

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §5 / §10「HBD-01」、
docs/enhance/202607-hearing-flow.md §3（質問セット Q1..Q6＋Auto）/ §4（回答→推薦）/ §6（決定ルールとGenAI補助の境界）/ §7（データモデル）。
→ 着手時に hearing-flow.md を `specs/16-platform.md` の一部へ昇格する（spec-driven）。

## 受け入れ条件（検証可能な述語で書く）
- [ ] migration で `hearing_session` / `hearing_answer` / `recommendation`（hearing-flow §7 のフィールド）を追加し、冪等再適用が成功する
- [ ] 質問スキーマ（Q1..Q6＋Auto、選択肢、回答型）をコード/JSON で定義し、スキーマ検証テストが通る
- [ ] 推薦ルールエンジン: 回答→素材マッピング（§3/§4 の対応表）を**決定的関数**として実装し、代表ケースの単体テストが網羅的にパス（例: サポート＋文書＋RAG-QA→SBA-A＋{RAG-QA,要約,分類}）
- [ ] GenAI補助は §6 の境界に限定（①メモ要点抽出→デフォルト提案 ②「その他/複合」時の最近傍SBA提案 ③シード生成方針 ④サマリ文章化）。GenAI 不在/失敗でも決定ルールだけで推薦が成立する（フォールバック）
- [ ] API: ヒアリングセッション CRUD ＋ 回答保存 ＋ `POST .../recommend`（決定的推薦＋任意でGenAI補助）を提供し、既存の公開シグネチャを壊さない
- [ ] api lint（ruff）・型・既存テスト後方互換がクリーン

## E2E シナリオ（実環境 / jetuse-dev・複数）
完了ゲートで jetuse-dev の固定 loop 環境へデプロイ（DB タスク=専用スキーマ `JETUSE_HBD-01` で隔離）し、証跡を `runs/<run-id>/e2e/` に残す。
- [ ] シナリオ1（正常系）: セッション作成→Q1..Q6 回答保存→`recommend` 呼出で「主SBA＋AI部品＋コネクタ＋UI＋シード方針」が決定ルール通りに返る（実 ADB の永続を確認）
- [ ] シナリオ2（GenAI補助）: ヒアリングメモ貼付→実 GenAI（ap-osaka-1）で要点抽出→各質問のデフォルト提案が `source=genai_suggested` で保存される
- [ ] シナリオ3（フォールバック/境界）: GenAI を無効化しても決定ルールのみで推薦が成立する／「その他」業務で最近傍SBA提案に落ちることを確認
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由明記

## 成果物
migration＋`jetuse_core` の質問スキーマ/推薦エンジン / API ルート / `specs/16-platform.md`（hearing-flow 昇格）/ `docs/verification/HBD-01.md`

## 非ゴール / 制約
- ダイアログUI は HBD-02、合成（実際のデモ構成生成）は HBD-03、バリデーションは HBD-04。本タスクは「保存＋推薦の素地」まで。
- 推薦の最終選定は必ず画面で SA に提示する前提（ブラックボックス化しない）。本タスクは API までで UI は最小疎通のみ。
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案。既存リソースは参照のみ。コミット/PR/push は人間承認後。

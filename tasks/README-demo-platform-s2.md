# ステージ2 タスク索引（ヒアリング駆動スタンダードモード）

親計画: [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §5/§9/§10。
質問セット素案: [`../docs/enhance/202607-hearing-flow.md`](../docs/enhance/202607-hearing-flow.md)（HBD-01 着手時に `specs/16-platform.md` へ昇格）。
各タスクは `LOOP_TASK=<id> GOAL="..." claude` で1本ずつループ実行する（[`../LOOP.md`](../LOOP.md)）。進捗キュー: [`STAGE2-PROGRESS.md`](STAGE2-PROGRESS.md)。

## 目的（経路2＝新規構成ビルダー）
フィールドSAが顧客ヒアリング結果から、サンプル業務アプリ＋AI機能＋コネクタを**素材**に、
ガバナンス枠内で業務に即したデモを短時間で組み立てる体験を成立させる（S1+S2 で体験完成）。

## タスク
| ID | 内容 | 依存 | area |
|---|---|---|---|
| HBD-01 | ヒアリングフロー＆質問スキーマ（構造化保存）＋推薦ルールエンジン（決定的＋GenAI補助） | ステージ1 | both(api中心) |
| HBD-02 | ダイアログ式UI（順次Q&A・回答保存・進捗） | HBD-01 | web |
| HBD-03 | 合成エンジン（sample-app×AI部品×connector→デモ構成）＋プレビュー | HBD-01 | both |
| HBD-04 | 合成バリデーション（許可組合せ・必要ケイパ・権限スコープ） | HBD-03 | api |
| HBD-05 | 構成サマリ出力（顧客提示用）＋E2E（ヒアリング→デモ起動） | HBD-02,03,04 | both |

## 推奨実行順
HBD-01 →（HBD-02 と HBD-03 を並行）→ HBD-04 → HBD-05（出口＝S1+S2 体験の一気通貫判定）。

## 人間ゲート（停止して承認を待つ）
コミット/PR/push（全タスク）／spec 昇格（HBD-01: hearing-flow→specs/16-platform.md）／デモ品質（HBD-05）。

## ガバナンス（§4 の4制約を弱めない）
固定リファレンス基盤（触らせない）／制約付きパレット（コア＋審査済みのみ）／合成バリデーション（HBD-04）／デプロイ上限＝コンテナ（S4）。

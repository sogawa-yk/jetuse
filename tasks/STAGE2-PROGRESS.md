# ステージ2 進捗キュー（loop-runner の単一の真実源）

ヒアリング駆動スタンダードモード（経路2＝新規構成ビルダー）。
`loop-runner` スキルが依存順に消化する。status を更新するのは loop-runner（人間がゲートを通した後）。
詳細は各 `tasks/<id>.md`、索引は [`README-demo-platform-s2.md`](README-demo-platform-s2.md)、
親計画は [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §5/§9/§10。

status: `todo` | `in_progress` | `blocked` | `done`

前提: ステージ1 完了（PLG-01..08 / SBA-01,02 / SBA-03,04 = 12/13 done。SBA-05 のみ MM-01(VLM) 待ちで blocked）。

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | HBD-01 ヒアリングフロー＆質問スキーマ＋推薦ルールエンジン | ステージ1 | コミット / spec昇格 | todo |
| 2 | HBD-02 ダイアログ式ヒアリングUI（順次Q&A・回答保存・進捗） | HBD-01 | コミット | todo |
| 3 | HBD-03 合成エンジン（sample-app×AI部品×connector）＋プレビュー | HBD-01 | コミット | todo |
| 4 | HBD-04 合成バリデーション（許可組合せ・必要ケイパ・権限スコープ） | HBD-03 | コミット | todo |
| 5 | HBD-05 構成サマリ出力＋E2E（ヒアリング→デモ起動） | HBD-02,03,04 | デモ品質 | todo |

> 並行可: HBD-01 完了後、HBD-02 と HBD-03 は相互独立で並行可（最大3）。HBD-04 は HBD-03 後、HBD-05 は HBD-02/03/04 後。
> 単一セッションの loop-runner は「依存が満たされた todo の先頭」を1つずつ実行する。

> **実行方式の選択**:
> - `loop-runner`: 波ごとに人間ゲート（コミット/PR/承認）で停止するセミオート（従来）。
> - `stage-runner`（`.claude/loop/start-stage.sh stage-2`）: ステージ承認ループ。PASS タスクを
>   `feat/stage-2` へ自動統合して波を繋ぎ、**ステージ完了で1回だけ報告**。この方式では status 更新は
>   **Codex PASS＋自動統合後**に行い（人間承認を待たない）、デモ品質/ADR 承認/push/PR/apply は
>   ステージ報告（`runs/_stages/stage-2/REPORT.md`）に集約して人間に提示する。

## 実行可能集合（開始時）
- HBD-01 のみ（S2 の先頭）。完了後に {HBD-02, HBD-03} が解禁。

## 人間ゲート（停止して承認を待つ）
- コミット / PR / push（全タスク共通）
- spec 昇格: HBD-01 着手時に hearing-flow.md を `specs/16-platform.md` へ昇格（spec-driven の確定）
- デモ品質: HBD-05（ヒアリング→デモ起動の一気通貫品質を人間確認）

## 実行ログ（loop-runner が追記）
- 2026-06-26 ステージ2 起票: ステージ1 完了（12/13、SBA-05=MM-01待ち blocked）を確認し、
  202607-demo-platform-plan.md §10 ＋ 202607-hearing-flow.md を基に HBD-01..05 を `tasks/` へ落として本キューを作成。

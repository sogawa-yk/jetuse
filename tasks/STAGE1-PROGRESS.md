# ステージ1 進捗キュー（loop-runner の単一の真実源）

`loop-runner` スキルが依存順に消化する。status を更新するのは loop-runner（人間がゲートを通した後）。
詳細は各 `tasks/<id>.md`、索引は [`README-demo-platform-s1.md`](README-demo-platform-s1.md)、引き継ぎは [`../HANDOVER.md`](../HANDOVER.md)。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | PLG-01 manifest仕様＋バリデータ | — | ADR-0013 承認 | todo |
| 2 | PLG-02 データモデル(installed_plugins) | PLG-01 | コミット | todo |
| 3 | PLG-03 取込＋署名検証＋スナップショット | PLG-01,02 | コミット | todo |
| 4 | PLG-04 中央レジストリService(planまで) | PLG-01 | apply・課金 | todo |
| 5 | PLG-07 コントリビューションローダー | PLG-02,03 | コミット | todo |
| 6 | SBA-01 sample-app構造定義 | PLG-01 | コミット | todo |
| 7 | SBA-02 AI組込FW＋SBA-A 問い合わせ(RAG) | SBA-01,PLG-07 | デモ品質 | todo |
| 8 | PLG-05 公開フロー(export→署名→publish) | PLG-01,04 | コミット | todo |
| 9 | PLG-06 マーケットUI | PLG-03,04 | コミット | todo |
| 10 | SBA-03 SBA-B 在庫照会(NL2SQL) | SBA-02 | コミット | todo |
| 11 | SBA-04 SBA-C 営業案件(エージェント複合) | SBA-02 | コミット | todo |
| 12 | SBA-05 SBA-D 帳票(VLM-OCR) | SBA-02,MM-01 | VLM前提・コミット | todo |
| 13 | PLG-08 MVP E2E(横断共有) | PLG-04..07 | デモ承認 | todo |

> 並行可（別セッションで人間が回す場合）: PLG-04 は PLG-02/03 と並行可。SBA-03/04/05 は SBA-02 後に相互並行可。PLG-05/06 は PLG-04 後に並行可。
> 単一セッションの loop-runner は「依存が満たされた todo の先頭」を1つずつ実行する。

## 実行ログ（loop-runner が追記）
- （未開始）

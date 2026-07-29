# シナリオ4（安全ガードの否定シナリオ）— PASS

review-3 で「誤接続・既存リソース衝突・teardown の安全性を確認する否定シナリオが無い」と
指摘されたため追加。「壊さない設計にした」ではなく、**わざと危ない状況を作って止まることを
実行結果で示す**。

## 実行コマンド

```
PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/guard_checks.py
```

生ログ全文: `guard-checks.log`

## 実結果

| | 危ない状況 | 期待 | 実結果 |
|---|---|---|---|
| G1 | 接続先が想定の ADB でない | DDL 前に中止 | exit=1 `想定外の接続先 DB_NAME=…（想定 SOMEONE_ELSES_DB）… 中止。` |
| G2 | 同名スキーマが存在するが台帳に無い | ALTER/GRANT/ACL を打たず中止 | exit=1 `ユーザー JETUSE_SPIKE_M1 は既に存在するが台帳に無い。… 中止する。` |
| G3 | 台帳が空の状態で `teardown.py --yes` | 名前一致だけでは何も消さない | 5 箇所すべて `名前一致では消さない（スキップ）`。`DROP USER` も `delete bucket` も実行されず |

```
PASS: G1/G2/G3 いずれもガードが働き、危険な操作の手前で止まった
```

## この確認自体が一度事故を起こしている（記録）

G3 の初版は **OCI 側だけを台帳ゲートにしており、DB 側（`drop_db_objects` / `drop_schema`）は
名前一致で消していた**。そのため `teardown.py --yes` を空台帳で走らせた G3 の実行が、
本物の `JETUSE_SPIKE_M1` スキーマを実際に削除した（`all_users` で 0 件を確認）。

- 影響範囲: スパイク専用スキーマのみ。共有 ADB の他スキーマ・他リソースには波及していない
  （`assert_target` と `DROP USER <SCHEMA>` の対象限定が効いていたため）。
- 対処: `drop_db_objects` / `drop_schema` にも台帳ゲートを入れ、スキーマを作り直して
  ③②のシナリオ証跡を全て取り直した（本ディレクトリのログはすべて再取得後のもの）。
- 教訓: 「消してよいものの判定」を OCI 側と DB 側で別ロジックにすると、片方だけ抜ける。
  Codex の blocker 指摘（名前一致での削除）は OCI 側だけを指していたが、
  **同じ穴が DB 側にも空いていた**。

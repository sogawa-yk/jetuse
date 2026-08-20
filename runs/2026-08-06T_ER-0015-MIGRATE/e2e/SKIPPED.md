# 実環境検証の範囲（ER-0015）

**実 OCI での検証は実施した。** 対象外にしたのは「共有スキーマを壊して再現する」経路だけで、
それも代替手段で塞いだ。当初この文書は「実 OCI に配備しても新しく動く経路が無い」と書いていたが、
**これは誤り**（Codex review-4 の blocker）。差分は `/api/health` が毎リクエスト実 DB の
`user_tables` と `schema_migrations` を読む**新しい経路**を足している。

## 実環境で確かめたこと

対象 ADB: `jetuse-loop-adb`（ap-osaka-1・`.env` が指す先）。この検証のため STOPPED から起動した。
接続は `.env` のまま（`ADB_WALLET_DIR` だけ空にしてウォレットを ADB_OCID から再生成 —— `/tmp` の
キャッシュが PC 再起動で消えていたため）。

| | 内容 | 結果 | 記録 |
|---|---|---|---|
| シナリオ7 | `applied_versions()` が実 Oracle を読む / **非 DDL** / `call_timeout` | 21 件を 2.7s で読み取り。診断を3回まわしても `user_tables` 21→21・`schema_migrations` 21→21。`conn.call_timeout=10000` | `07_real_adb_readonly.txt` |
| シナリオ8 | `ok` / `behind` / `foreign` の3状態を実 DB のまま出す | ok(21/21) → behind(pending 1件・`ok=false`) → foreign(余り 1件・`ok` は落とさない)。**DB は前後で不変** | `08_real_adb_states.txt` |
| シナリオ9 | HTTP ルート込みで `GET /api/health` | **200 / 3.58s**・`schema={"status":"ok","applied":21,"expected":21}` | `09_http_health.txt` |

### 共有スキーマを壊さずに behind / foreign を出した方法

ER-0015 は「**DB は据え置きで、イメージだけ先に進んだ**」状態なので、動かすべきは DB ではなく
イメージ側だった。`migrations/` に1本足せば `behind`、1本外せば `foreign` が実 DB を読んだまま再現する。
`jetuse-spike-` スキーマを作る必要も、既存スキーマを不整合にする必要も無い。
**シナリオ8の D が「DB が前後で一切変わっていない」ことを実測している。**

## ローカルで確かめたこと

| | 内容 | 記録 |
|---|---|---|
| シナリオ1 | 単体 35 件（health 12 / ランナー・判定 23） | `01_pytest.txt` |
| シナリオ2 | **変異検査 5 種**。`pending` を殺す→5件 FAIL / `ok` に効かせない→1件 / ランナー警告を消す→4件 / 差の向きを反転→12件 / `call_timeout` を張らない→1件 | `02_mutation.txt` |
| シナリオ3 | **CLI を別プロセスで起動**し stderr を確認。logger に出ていても、CLI にログ設定が無ければ人に届かない | `03_cli_stderr.txt` |
| シナリオ4 | 実ブランチの migration 集合を照合（`internal-dev` ⊃ `public-dev`・差分 11 本・public 専用 0 本） | `04_branch_sets.txt` |
| シナリオ5 | `RUN_DB_BOOTSTRAP` の設定箇所を全リポジトリで数え、非対称を確認 | `05_bootstrap_asymmetry.txt` |
| シナリオ6 | **internal-dev 相当の checkout を実際に作って**（`checkout_versions()` が 21→32）テストが通ることを確認。撤去して原状復帰 | `06_internal_checkout.txt` |

## レビューで潰した点

| 版 | 指摘 | 実際どうだったか |
|---|---|---|
| review-1 blocker | `foreign_versions`（DB − checkout）では実害を検出できない | **そのとおり**。実害時は DB も checkout も Public 集合で差が 0。`pending_versions`（イメージ − DB）を `/api/health` に置いて塞いだ。シナリオ2の変異1が初版の状態を再現して 5 件落ちる |
| review-2 blocker | テストが `PUBLIC = checkout_versions()` と書いており `internal-dev` で成り立たない | **そのとおり**。sync 後に CI が壊れる。集合を素の一覧で持つよう直し、シナリオ6で internal 相当の checkout を実際に作って確認した |
| review-2 major | 診断経路が `call_timeout` を迂回しており、ADB 停止時に `/api/health` が返らない | **そのとおり**。`db.connect()` 経由に直した（変異5で検知・シナリオ7で実測） |
| review-2/4 minor | 警告・hint が影響を断定していた（「表が足りない」「503 のまま」） | **そのとおり**。DB が先行しているだけなら壊れているとは限らず、未適用が index だけなら機能は動く。条件付きの言い方に直した |
| review-4 blocker | 「実 OCI で新しく動く経路が無い」は嘘で、正常系の読み取り検証が無い | **そのとおり**。シナリオ7〜9 を実施した（本文書の上半分） |

## 副産物

**ADB が3つとも STOPPED だった**（`jetuse-dev-adb`/us-chicago-1・`jetuse-loop-adb`/ap-osaka-1・
`.env` の指す ADB）。ER-0018（共有 ADB が予告なく停止していた）が現在進行形であることの追加証拠。

## 証跡の伏せ字

シナリオ7の出力にあった接続ユーザー名は `<開発者スキーマ>` に伏せてある。
`runs/` は git に載るため、案件を推測させうる固有名を残さない（CLAUDE.md の公開リポジトリ規約）。
検証内容には影響しない（読んでいるのは `schema_migrations` の version 一覧だけ）。

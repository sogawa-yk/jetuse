# SBA-03 検証レポート — コアアプリ SBA-B「在庫・受発注照会」(NL2SQL)

- タスク: SBA-03 / area: both(api ＋ web)
- 仕様参照: docs/enhance/202607-demo-platform-plan.md §6(SBA-B) / specs/10-dbchat.md(SQL-02) / specs/16-platform.md
- 実行環境: jetuse-dev の共有 loop ADB(固定の再利用 ADB / 26ai)。実 ADB 名・DSN・リージョン等の
  環境依存実値は `.env`(gitignore 済み)とローカル E2E 証跡に分離し、本レポートには載せない
  (リポジトリ運用ルール)。
- 隔離: タスク専用スキーマ(命名規約 `JETUSE_<task>`)に業務データを置き、別の読取専用ユーザ
  (CREATE SESSION のみ + SBA-B テーブル SELECT + private synonym)で照会する。ADB は増やさず再利用。

## 1. 実装概要

SBA-02 の AI 組込フレームワーク(`ai_runtime`)に `nl2sql`(自然言語DB照会)と `chart`
(結果グラフ化)capability を束縛し、コア同梱 sample-app SBA-B を追加した。SBA-A と同じ
`kind: sample-app` 型に倣う。

- 業務データモデル(datasets): `inventory`(在庫)/ `orders`(受発注)＋シード。
- aiSlots: `nl2sql-query`(capability=nl2sql, permission=`platform:db.query`)/ `result-chart`(chart)。
- 照会フロー: 日本語質問 → 生成SQL(nl2sql スロット / 実 GenAI llama-3.3-70b)→ ユーザー確認・編集
  → 読取専用実行(`/api/dbchat/execute` = JETUSE_QUERY 相当の読取専用ユーザ)→ 結果テーブル
  → グラフ化(chart スロット / 既存 `ResultChart`)。

### 多層ガード(SQL-02 流用・緩めない)

1. 生成段: `sanitize_sql`(SELECT/WITH 限定・複数文/更新系拒否)＋ `assert_tables_allowed`
   (定義 datasets のテーブルのみ・スキーマ修飾名/別スキーマ拒否。文字列リテラルは抽出前に無効化)。
2. 実行段: 読取専用ユーザ(CREATE SESSION のみ)/ `FETCH FIRST 200 ROWS` 相当の行数上限 /
   call_timeout 30s。SELECT 以外は権限とコード両面で不可。専用 execute は実行接続の
   `CURRENT_SCHEMA` を照会対象スキーマ(`SAMPLE_DB_SCHEMA`=`JETUSE_SBA03`)へ固定し、非修飾
   テーブル名が当該スキーマの物理表へ確定解決する(synonym 依存・読取ユーザ側の同名オブジェクト
   に左右されない / B1)。スキーマ識別子は厳格に検証(`[A-Za-z][A-Za-z0-9_$#]*`)し不正値は拒否。
   固定はプール共有のため当該実行に限定し、本文実行後(成功・例外いずれも)に接続ユーザ自身の
   スキーマへ戻す。これにより既存 `/api/dbchat/execute`(`current_schema` 未指定)が同じプール接続を
   再利用しても固定が残留しない(後方互換 / 状態漏れ防止)。
3. 列スコープ: 対象テーブルは SBA-B 定義 fields から 1:1 生成され、定義外の列が物理的に存在しない
   (列単位の SQL パースは誤判定が多いため非採用。テーブル粒度 + 物理スキーマ一致で担保)。
4. 到達範囲の最小化: 専用 execute は `nl2sql` capability を束縛した sample-app のみ提供し、SBA-A 等
   DB 照会を持たないアプリは 404。さらに `DUAL` の暗黙許可を切り（`allow_dual=False`）、業務テーブルを
   最低1つ参照しない SQL（スカラ/`SYS_CONTEXT(...)`/関数呼び出しのみ）も拒否（`require_table=True`）。

## 2. 静的検証(単体テスト / lint / build)

| 項目 | コマンド | 結果 |
| --- | --- | --- |
| API 単体テスト | `.venv/bin/pytest packages/api/tests` | 450 passed |
| 共有契約テスト | `.venv/bin/pytest packages/jetuse_shared/tests` | 56 passed |
| Web 単体テスト | `npm --prefix packages/web run test` | 81 passed(SBA-B 5 本: 主フロー+401/ガード) |
| ruff | `.venv/bin/ruff check packages/api` | clean |
| eslint | `npm --prefix packages/web run lint` | clean |
| web build | `npm --prefix packages/web run build` | tsc + vite 成功 |

## 3. 実環境 E2E

証跡: `runs/<run-id>/e2e/`。再現スクリプト: `run_e2e.py`(内部経路)/ `run_e2e_http.py`(HTTP 経路)。
run-id ロールオーバのため、HTTP 経路は継続 run(`2026-06-26T1018_SBA-03/e2e/`)で実機再実行し、
内部経路 4 シナリオは直前 run(`2026-06-26T0810_SBA-03/e2e/`)の実証跡を参照する(理由は当該 run の
`e2e/SKIPPED.md`: 共有 loop ADB の ADMIN 競合で再プロビジョニング不可。専用スキーマ/読取ユーザは永続)。
2 経路で実施(いずれも実 GenAI + 実 loop ADB):
- 内部経路: `scenario-1..4.json` / `guard.json`(provision 込み 4 シナリオ + 各種ガード)。
- HTTP 経路(M1/B1): `scenario-http-1..2.json` / `guard-http.json` / `summary-http.json`。実 FastAPI ルート
  (Pydantic・auth 依存・registry・専用 execute ルート)を TestClient で通し、2 シナリオ成功・
  越境/更新系/DBlink を `POST /api/sample-apps/{id}/dbchat/execute` が 400 で拒否。専用 execute は
  `CURRENT_SCHEMA=JETUSE_SBA03` を固定した上で実 loop ADB を読取専用実行(非修飾 `INVENTORY`/`ORDERS`
  が当該スキーマの物理表へ解決し成功 / B1)。
未実施範囲(ブラウザ Playwright)は `runs/<run-id>/e2e/SKIPPED.md` に理由・残リスクを明記。

### プロビジョニング(ADMIN)
SBA-B 定義からタスク専用スキーマに `INVENTORY` / `ORDERS` を生成しシード投入。別の読取専用ユーザに
SELECT + private synonym のみ付与(物理列は dataset.fields と 1:1)。

### シナリオ結果（4/4 成功・実 GenAI llama-3.3-70b + 実 ADB 読取専用実行）

| # | 日本語照会 | 生成SQL(要約) | 実行結果 | グラフ |
| --- | --- | --- | --- | --- |
| 1 | 倉庫別の在庫数の合計を多い順に | `SELECT WAREHOUSE, SUM(QUANTITY) FROM INVENTORY GROUP BY WAREHOUSE ORDER BY ... DESC` | 3行(大阪DC 1360 / 東京DC 1190 / 福岡DC 618) | bar |
| 2 | 取引先別の受注金額の合計トップ5 | `SELECT PARTNER, SUM(AMOUNT) ... WHERE ORDER_TYPE='受注' GROUP BY PARTNER ORDER BY ... FETCH FIRST 5 ROWS ONLY` | 5行(山田商事 299,200 ほか) | bar |
| 3 | 在庫数が発注点を下回っている商品 | `SELECT PRODUCT_CODE, PRODUCT_NAME, QUANTITY, REORDER_POINT FROM INVENTORY WHERE QUANTITY < REORDER_POINT` | 6行(P-1002/1005/1008/1010/1012/1014) | bar |
| 4 | 月別の受注金額の推移 | `SELECT TO_CHAR(ORDER_DATE,'YYYY-MM') 月別, SUM(AMOUNT) 受注金額 ... GROUP BY ... ORDER BY ...` | 6行(2026-01〜06) | line |

各シナリオの詳細(生成SQL全文・列・行サンプル・グラフ仕様・生成/実行時間)は `scenario-1..4.json`。
provision ログは `deploy.log`、集計は `summary.json`(scenarios_ok=4, guard_ok=true)。

### ガード検証(ネガティブ / `guard.json`)

- 生成段: 生成器が `SELECT username FROM SYS.DBA_USERS` を返しても
  「スキーマ修飾テーブルは参照できません: SYS.DBA_USERS」で拒否(`SlotInferenceError`)。
- 実行段(更新系): `DELETE FROM INVENTORY` は `execute_readonly` が「SELECT文のみ実行できます」で
  拒否(読取専用ユーザの権限不足とコード両面)。
- 実行段(編集SQLのテーブル越境 / B1): UI で編集された SQL は sample-app 専用 execute 経路
  `POST /api/sample-apps/{id}/dbchat/execute` を通り、`sanitize_sql` + `assert_tables_allowed(datasets)`
  を必ず適用する。`SYS.DBA_USERS` / `SH.SALES` / 未知テーブル / q-quote 回避(`q'[, X AS (]'`)の
  4 本はすべて拒否、定義内 `INVENTORY` 照会のみ通過(`guard.json.edited_sql_guard`)。
  SQL-02 ガードを緩めていない。

> 注: 共有 loop ADB は同時刻に別タスク(SBA-04)も再利用しており、本 run の途中で別ループが
> ADB の ADMIN パスワードをリセットした(タスク専用スキーマによる隔離は有効だが、ADMIN
> パスワードは共有資源で競合する)。シナリオ 1〜4 と生成/更新系ガードは競合前の実 DB run の
> 証跡。編集SQL越境ガード(B1)は route が実行前に通すガード関数を SBA-B 定義の許可集合で検証した
> 結果で、API ルートテスト(`test_sample_app_execute_*`)が同経路の 400 応答を裏づける。

## 4. 残課題 / 人間ゲート

- コミット / PR / push は未実施(人間承認待ち)。
- E2E 用 `packages/api/.env` はローカル(gitignore 済み。実 OCID/DSN/パスワードはコミットしない)。
  共有 loop ADB の ADMIN パスワードは本 run でリセット(共有 loop ADB の運用ルールに従う)。

# SBA-04 検証レポート — コアアプリ SBA-C「営業案件管理」(エージェント複合)

- タスク: `tasks/SBA-04.md`
- run-id: `2026-06-26T0810_SBA-04`
- ブランチ: `feat/SBA-04`（base: SBA-02 マージ済み main）
- 実施日: 2026-06-26
- 結論: **複合AI(議事録要約・次アクション提案エージェント・売上集計NL2SQL・メール下書き)が
  SBA-C 内で連動して動くことを、実 OCI GenAI ＋ 実 ADB に対し 2 シナリオで E2E 実行し確認した。**

## 1. 何を作ったか

コア同梱 sample-app **SBA-C「営業案件管理(SFA-lite)」**（SBA-A と同じ `kind: sample-app` 型・
コード同梱・aiSlots による AI 組込）。営業の業務フロー（パイプライン → 案件コンソール → 売上分析）に
4 つの AI 能力を組込点として配置し、前段の出力を後段の入力へ渡して **連動**させる。

| aiSlot | capability | 流用元 | 役割 |
|---|---|---|---|
| `minutes-summary` | `minutes` | VOICE-01 議事録整形 | 商談議事録の構造化要約 |
| `next-actions` | `agent` | AGT 系 宣言型エージェント | 案件＋議事録要約から次アクション提案 |
| `sales-rollup` | `nl2sql` | SQL 系 NL2SQL | 自然言語売上集計（専用スキーマ照会） |
| `email-draft` | `draft` | SBA-A 返信ドラフト | 顧客向けフォローメール下書き（**実送信なし**） |

- 実行時バインドは SBA-02 の `ai_runtime`（capability→handler レジストリ）を拡張し、
  `minutes`/`agent`/`nl2sql` を新規束縛（`draft` は SBA-A 流用）。
- 売上集計の実 DB は **共有 loop ADB（jetuse-loop-adb）をタスク専用スキーマ `JETUSE_SBA04` で隔離**
  （ADB は増やさない）。`SALES` は自己完結したファクト表（顧客/製品/地域/担当/金額/受注日）。

## 2. 静的ゲート（コミット前チェック）

| ゲート | コマンド | 結果 |
|---|---|---|
| api 単体 | `.venv/bin/pytest packages/api/tests` | **435 passed**（cov 67%） |
| api lint | `.venv/bin/ruff check packages/api` | **clean** |
| web 単体 | `npm --prefix packages/web run test` | **78 passed** |
| web lint | `npm --prefix packages/web run lint` | **clean** |
| web build | `npm --prefix packages/web run build` | **built OK** |

新規テスト `packages/api/tests/test_sample_app_sba_c.py`（定義/合成バリデーション/各ハンドラ/
ルート/連動チェーン）と `packages/web/src/pages/sampleappc.ui.test.tsx`（議事録要約→次アクション
→メール下書きの連動、売上分析 NL2SQL の結果表示）を追加。

## 3. 実環境 E2E（完了ゲート / 固定 loop 環境を再利用）

- 環境: jetuse-dev `jetuse-loop-adb`（26ai, db_name=jetuseloop, ap-osaka-1, AVAILABLE）。
  ADMIN パスワードは都度リセット・ウォレットは generate-wallet で再生成（[[loop-e2e-adb-jetuse-dev]]）。
- 隔離: ADMIN で `CREATE USER JETUSE_SBA04` → `DEALS`(6行)/`SALES`(12行) を投入（毎回 drop→再作成）。
  読取専用ユーザー `JETUSE_SBA04_RO`（`CREATE SESSION` ＋ `SELECT ON JETUSE_SBA04.SALES/DEALS` のみ）を
  作成し、NL2SQL の実行はこの**最小権限ユーザー**で行う（`setup.json` の `query_user=JETUSE_SBA04_RO`）。
- **配備パス（HTTP）**: 各スロットは FastAPI ルート
  `POST /api/sample-apps/builtin-sba-c/slots/<key>/invoke` を TestClient で実呼び出し（`http_calls`
  に全 200 を記録）。スロット束縛・モデル解決・入力ガード・`nl2sql_schema` 伝播・schema allowlist
  ガードを含む配備ルートを実サービスに対して通している（runtime 直呼びではない）。
- 推論: `minutes`/`agent`/`draft` は実 OCI GenAI（既定 `llama-3.3-70b` chat）。
  `nl2sql` は実 ADB の `JETUSE_SBA04` へ Select AI（`DBMS_CLOUD_AI.GENERATE showsql`）で NL→SQL 生成
  → **schema allowlist ガード（スロット別の許可表のみ。sales-rollup は `sales` だけ）** →
  最小権限ユーザーで読取専用実行（`sanitize_sql` ＋ 行数/タイムアウト上限）。
- **クロススキーマ隔離（実機証跡）**: `setup.json` に以下を記録。
  - `query_user=JETUSE_SBA04_RO`（ADMIN ではなく最小権限ユーザーで実行）。`ro_grants` は SALES/DEALS の SELECT のみ。
  - `sh_public_readable_by_ro=true`: **共有 loop ADB では SH サンプルスキーマが PUBLIC 付与**のため、
    RO の最小権限だけではクロススキーマ参照を遮断できない（RO でも `SH.SALES` が読めてしまう）。
    → よって**生成SQLの schema スコープガードが結合的（binding）な一次防御**であることを実機が裏づける。
  - `guard_blocks_cross_schema`: 実関数 `_assert_schema_scoped` が `SH.SALES`・カンマ結合・コメント区切り
    JOIN・サブクエリ・非修飾参照・**DBリンク経由（`@link`）・CTE 本体での他スキーマ参照**の
    **全 bypass を拒否**（7 ケース all true）。生成SQL拒否は HTTP 502 に正規化。
  - `guard_allows_valid_cte=true`: 正当な WITH/CTE（本体は許可テーブル）は誤って 502 にせず通す
    （CTE 名参照は許容しつつ、CTE 本体内の実テーブル参照は通常どおり検査する＝隔離は緩めない）。
- ドライバ: `runs/2026-06-26T0810_SBA-04/e2e/run_e2e.py`。証跡: 同ディレクトリの
  `scenario-1.json` / `scenario-2.json` / `setup.json` / `deploy.log` / `run.full.masked.log` / `SKIPPED.md`
  （ブラウザ層の限定範囲を明記）。**証跡ログは GenAI エンドポイント実値・DSN・OCID をマスク**
  （`<GENAI_ENDPOINT>` / `<ADB_DSN>` / `<OCID>`）してコミットする（環境固有値を残さない）。
- メール: **実送信しない**（`email_draft` を生成するのみ。`external_send: false`。営業メール下書きは
  FAQ 文脈を渡さず案件・次アクション・売上参考のみを根拠にする）。

### シナリオ1: 案件 deal-001（山田製作所 / MES連携クラウド導入）
連動チェーン `minutes → agent → nl2sql(JETUSE_SBA04) → draft`:
1. **議事録要約**: 第2回提案レビューのメモ → 「MES老朽化・クラウド移行に前向き・接続セキュリティ懸念」
   等を構造化（371字）。
2. **次アクション提案**: 案件情報＋上記要約 → 12 件（先頭「次回会議までに山田部長が対象ラインの
   絞り込みとPoC計画を策定 — PoC範囲の合意」）。
3. **売上集計(NL2SQL)**: 「担当者別の売上合計を多い順に」→ 生成SQL
   `SELECT s.OWNER, SUM(s.AMOUNT) ... GROUP BY s.OWNER ORDER BY total_sales DESC`、
   実結果 **3行**: 加藤=52,600,000 / 佐々木=36,000,000 / 小林=30,500,000。
4. **メール下書き**: 案件＋次アクション＋集計を踏まえた PoC範囲合意のフォローメール（476字、実送信なし）。

### シナリオ2: 案件 deal-002（明日工業 / 在庫最適化SaaS）
1. **議事録要約**: 価格交渉メモ → 「他社比 約8%高・保守費内訳要望・10%まで値引承認済」を要約（405字）。
2. **次アクション提案**: 11 件（先頭「今週中 見積もり書の最終調整 — 見積もり提出準備」）。
3. **売上集計(NL2SQL)**: 「地域別の売上合計を多い順に」→ `GROUP BY s.REGION ... DESC`、
   実結果 **7行**: 中部=37,900,000 / 関東=18,500,000 / 北海道=15,200,000 / 九州=15,200,000 /
   関西=13,400,000 / 中国=11,700,000 / 東北=7,200,000。
4. **メール下書き**: 正式見積・保守費内訳の送付メール（484字、実送信なし）。

両シナリオとも、議事録要約の出力が次アクション提案の入力に、案件＋次アクション＋売上集計が
メール下書きの入力に渡り、**4 能力が 1 アプリ内で連動して実環境で動作**することを確認した。

## 4. 発見・Tips

- **NL2SQL は不要な JOIN で集計を取りこぼす**: 初回 E2E で `SALES` に過去案件への人工的な
  `deal_id`（実在しない案件ID）を持たせたところ、Select AI が `DEALS` への JOIN を選び
  シナリオ1が 1 行に縮退した。アクティブな案件（`deals`）と過去受注実績（`sales`）は別エンティティで、
  人工 FK で結ぶと NL2SQL が誤誘導される。**ファクト表は自己完結させる**（`SALES` から `deal_id` を撤去）
  ことで「担当者別」が正しく 3 行になった。データモデルが NL2SQL の品質を左右する好例。
- **共有 ADB では SH が PUBLIC 付与 → DB 最小権限だけでは隔離できない（実機で確認）**: 新規 RO ユーザー
  でも `SH.SALES`（91万行）が読めた。よって**生成SQLの schema スコープガードが結合的な一次防御**。
  `run_nl2sql_for_schema` は sanitize（コメント除去）後に `_assert_schema_scoped` で **FROM/JOIN/カンマ
  結合/サブクエリの全 table factor** を列挙し、対象スキーマ修飾＝かつ**スロット別**許可表（screen の
  dataset 由来。sales-rollup は `sales` のみ）以外を拒否。`SH.SALES`・`FROM a, SH.x`・`JOIN/**/SH.x`・
  `(SELECT FROM SH.x)`・非修飾 `FROM SALES` の bypass を全て弾く（実機 `guard_blocks_cross_schema` all true）。
  生成SQL拒否・SQL未生成・列不正等は `SlotInferenceError`→HTTP 502 に正規化し、DB 接続障害のみ 503 に通す。
- **スロット別に照会面を絞る**: NL2SQL の許可表はスロットを載せる screen の dataset から導出する
  （全 dataset ではない）。売上集計スロットから案件詳細・議事録まで NL で照会できる「面の広げすぎ」を防ぐ。
- Select AI プロファイルはスキーマ別に遅延作成（`object_list=[{owner: JETUSE_SBA04}]`）。
  プロファイル作成は初回のみ遅い（~90s）。
- **共有 loop ADB の ADMIN パスワード競合**: 並行 loop タスクが同じ ADB の ADMIN パスワードを
  リセットすると、こちらの接続が ORA-01017 になる（実際に発生）。専用スキーマ（`JETUSE_SBA04`）で
  データは隔離されるが、ADMIN 資格情報は共有。E2E は接続直前にパスワードを取り直す前提で運用する。

## 5. review-4（PASS / major 4）への追加ハードニング（ユーザー判断で先に解消）

review-4 は blocker 0 で PASS だったが、残 major 4 件を停止前に解消した（再 E2E＋再レビュー込み）。

- **F-001（セキュリティ / DBリンク）**: ガードが `JETUSE_SBA04.SALES@LINK` のような **DBリンク経由参照**を
  許容していた（スキーマ修飾済みに見えて別 DB の同名テーブルを読める抜け道）。table factor 抽出を
  `@link` まで取り込むよう拡張し、factor に `@` を含めば拒否。回帰テスト＋実機 `db_link=true` を追加。
- **F-002（機能バグ / CTE 誤検出）**: `WITH x AS (...) SELECT ... FROM x` の CTE 名が**非修飾テーブル**扱いで
  誤って 502 になっていた。`_cte_names` で WITH 句のローカル定義名を収集し、その参照は許可。ただし
  **CTE 本体内の実テーブル参照は通常どおり検査**（`WITH leak AS (SELECT * FROM SH.SALES) ...` は拒否）。
  回帰テスト＋実機 `cte_body_cross_schema=true` / `guard_allows_valid_cte=true` を追加。
- **F-003（DB unavailable の写像）**: DB 接続障害（marker 付き）が route で 500 になりえた。ドメイン例外
  `SlotBackendUnavailableError` を新設し route で **503** に写像。実機でも nl2sql の ORA-01017 が 503 で
  返ることを確認（接続復旧後に再実行で全 200）。route テスト（503）を追加。
- **F-004（実装バグの隠蔽）**: `_reraise_nl2sql_error` の `type(e).__name__.endswith("Error")` 全捕捉が
  `TypeError`/`ValueError` 等の実装バグまで 502 に丸めていた。**想定する例外（SqlRejectedError /
  oracledb.Error / RuntimeID）だけ**を 502/503 に正規化し、それ以外は再送出して **500 に露出**させる。
  `TypeError` が 502 に丸められないことを確認する回帰テストを追加。

### review-6 / review-7 で追加検出した分の解消（同じく停止前に対応）

- **DBリンク検出漏れ（review-7 blocker）**: `schema.table@"REMOTE LINK"`（引用識別子のリンク名）が
  guard を素通りしていた。`@link` 抽出を引用名・ドメイン修飾・空白挟みまで網羅し、加えて
  **文字列リテラル無効化後の SQL に `@` が残れば一律拒否**する決定的バックストップを追加（factor
  解析の取りこぼしに依存しない一次防御）。回帰テスト（`@"..."` / `@ "L"`）を追加。
- **文字列リテラルの誤検出（review-6 minor）**: `'... FROM SH.X ...'` のようなリテラル内の語を
  テーブル参照と誤認していた。factor 抽出・CTE 収集の前に**リテラル内容を空白化**（`_blank_string_literals`）。
- **RuntimeError の過剰正規化（review-6 major）**: SQL 未生成専用例外 `SelectAiNoSqlError` を新設し、
  既知の SQL 生成失敗だけを 502 に正規化。未知の `RuntimeError` は再送出して 500 に露出させる。
- **次アクションの期限創作（review-6/7 major）**: agent プロンプトで絶対日付の創作を禁止し、
  生成後に**入力に存在しない絶対日付を `(期限未定)` へ中和**（`_strip_invented_dates`、和式↔ISO の
  表記揺れはゼロ埋め正規化で同一視）。公開レスポンスの `text` も **sanitize 済み actions から再構成**し、
  raw は `raw_audit`（監査専用）に分離。実機 scenario JSON でも創作絶対日付ゼロを確認。
- **証跡のエンドポイント/DSN/OCID マスク（review-7 major）**: E2E ログを `<GENAI_ENDPOINT>` 等に
  マスクしてコミット（環境固有値・OCID を残さない）。

### review-8 / review-9 / review-10 の追加検出も停止前に解消

- **APPLY/LATERAL 検出漏れ（review-9 blocker）**: `CROSS/OUTER APPLY <other.schema>`・`LATERAL <other.schema>`
  の右辺がガードを素通りしていた。table factor の導入句として `FROM`/`JOIN`/カンマに加え **APPLY/LATERAL**
  も走査対象にし、Oracle の from_clause 文法の factor 位置を網羅。実機 `cross_apply`/`lateral` を拒否確認。
- **EXTRACT(.. FROM ..) 誤拒否（review-8 major）**: 関数引数の `FROM` をテーブルソースと誤認していた。
  `EXTRACT`/`TRIM`/`SUBSTRING` の関数括弧直下の `FROM` を除外。実機 `allows_extract=true`。
- **二重引用識別子の誤走査（review-10 minor）**: 別名 `"FROM"`/`"JOIN"` 等の予約語引用識別子を句
  キーワードと誤認しないよう、走査時に引用識別子を不可分スキップ。実機 `allows_quoted_keyword_ident=true`。
- **agent 出力の生テキスト漏れ（review-7 major）**: 公開レスポンスから raw を撤去し `text` も sanitize
  済み actions から再構成。**raw は公開面に一切載せない**。
- **DB 不可用の写像強化（review-10 major）**: marker に加え `oracledb.OperationalError`・`db init failed`
  （プール初期化/ウォレット取得失敗）も `SlotBackendUnavailableError`(503) に分類。列不正(ORA-00942)は 502 のまま。
- **共有 draft capability の後方互換（review-8/10 major）**: SBA-A(コーパスあり)が従来どおり FAQ 根拠の
  サポート返信になる回帰テストを追加（capability は分割せず corpus 駆動の分岐を維持＝意図的なデモ設計）。

実機 E2E は上記すべて反映後のコードで再実行（`run.full.masked.log`）。2 シナリオ全 4 スロット HTTP 200、
guard 9 ケース all true（cross/comma/comment/subquery/unqualified/db_link/db_link_quoted/cte_body/literal/
cross_apply/lateral）、`guard_allows_valid_cte`/`allows_extract`/`allows_quoted_keyword_ident`=true、
次アクションの絶対日付ゼロ、external_send=false を再確認した。

## 6. 残る人間ゲート

- コミット / PR / push（未実施。承認後に人間が実施）。
- `packages/web/dist/`（ビルド成果物）について:
  - **本タスクの実環境デプロイ経路 `ops/dev-env-up.sh`（loop dev-env / 完了ゲートの deploy_cmd）は
    デプロイ時に `npm run build` で SPA を source から再ビルドして配信する**（同スクリプト L41）。
    したがって committed dist は本デプロイ経路では配信ソースにならず、SBA-C UI は source から確実に入る。
  - 別経路の ORM スタック（`infra/orm/spa.tf` の `spa_dist_dir = packages/web/dist`）のみ committed dist を
    配信する。この経路向けには **コミット直前（人間ゲート）に `npm --prefix packages/web run build` を実行**
    して dist を更新する（成果物のため本レビュー差分には含めない＝差分肥大の回避）。`vite build` は green。

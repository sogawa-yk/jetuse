# STATE — RP-01（開発環境の DB 内資格情報をリソースプリンシパルへ統一）

- task: RP-01
- run_id: 2026-07-29T1605_RP-01
- branch: feat/RP-01（base: main。共有物のため dev ではなく main 起点）
- area: api
- review_verdict: **PASS**（review-14。blocker 0 / major 2 / minor 2・E2E adequacy=sufficient）
- last_review_ref: runs/2026-07-29T1605_RP-01/reviews/review-14.json
- updated_at: 2026-07-29

## やったこと

`ops/setup-select-ai.py` / `ops/setup-dev-schema.py` から `~/.oci/config` の手書きパース（4 箇所）と
`DBMS_CLOUD.CREATE_CREDENTIAL` を**削除**し、`DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL` に置換。
両スクリプトの共通部（接続解決・権限付与・ACL・RP 有効化）を `ops/_adb.py` に集約した。

- 接続先は環境変数 → `.env` → 既定値で解決（`ADB_DSN` / `ADB_WALLET_DIR` / `ADB_WALLET_PASSWORD`）。
  従来の `dev/terraform.tfvars` 読み出しはウォレットパスワードのフォールバックとして残す。
  これが無いと gitignore された tfvars が無い環境（この worktree）で ops を実行できず E2E できない。
- `setup-select-ai.py` に `--schema`（既定 `.env` の `ADB_USER`）。ADMIN 接続のみになり
  アプリ側パスワードが不要になった。
- 冪等化: `ENABLE_RESOURCE_PRINCIPAL` は PL/SQL 側で冪等（実機確認）。ユーザー作成は
  「既存ならパスワードを勝手に変えない（明示指定時のみ ALTER・ORA-28007 は続行）」。
  パスワード不明で再実行したときは migrate を理由付きでスキップし、exit 0。
- fail-closed: DDL の前に `assert_target()` で「SQL 接続先が `ADB_OCID` と同一 ADB」かつ
  「その ADB が `ADB_COMPARTMENT_OCID`（無ければ `COMPARTMENT_OCID`）**そのもの**」
  （OCID の完全一致）を確認。RP 有効化の直後には
  `DBA_TAB_PRIVS`（`owner='ADMIN'`）で EXECUTE が実際に付いたかを確認する。
  接続後の `call_timeout` も有限値を強制（`ADB_CALL_TIMEOUT_MS`・0 以下は拒否）。
- `settings.select_ai_credential` の既定を `OCI$RESOURCE_PRINCIPAL` へ（ADR §未解決 1 の結論）。
- `infra/terraform/environments/app` は `merge()` の**上書き側**で `SELECT_AI_CREDENTIAL` を注入。
- ドキュメント: `specs/09-rag.md` / `docs/guides/onboarding.md` / `docs/guides/dev-environments.md` /
  `docs/tips.md`（解消済みへ・ListBuckets の compartmentId 必須を追記）/ `.env.example` /
  ADR-0021 §未解決 1〜3 への結論追記（状態は Accepted のまま）/ `docs/verification/RP-01.md`。
- `spikes/spike_m1/common.py:oci_api_key()` は再現性のため残し、「ops はこの方式を使わない」旨を明記。

## E2E（実 ADB・共有 loop ADB / 検証用スキーマ `JETUSE_SPIKERP01` で隔離）

検証が作る資源の名前は **run 固有**（この run は `rp01d389` → `JETUSE_RP01D389` /
`jetuse-spike-rp01d389-rag` 等）。固定名による所有権の取り違え（TOCTOU）を構造的に潰した。

| # | シナリオ | 結果 | 証跡 |
|---|---|---|---|
| 1 | ops 2 本を同じスキーマへ 2 回連続実行（冪等） | PASS | `e2e/scenario-1.md` |
| 2 | RP で `DBMS_CLOUD.SEND_REQUEST` → Object Storage 200 | PASS | `e2e/scenario-2.md` |
| 3 | RP でベクトル索引の作成・取り込み・検索（合言葉で照合） | PASS | `e2e/scenario-3.md` |
| 4 | 対照: 旧パーサ由来の API キー資格情報で ORA-20404 | PASS | `e2e/scenario-4.md` |
| 否定 | 台帳が一致しないと片付けが何も消さずに止まる（5 ケース。うち 2 件は USER_ID による同秒作り直しの検出） | PASS | `e2e/guard.md` |

1→guard→2→3→4→teardown を通しで 1 回実行した証跡。検証用リソースは `teardown.md` のとおり
削除し、削除後の再照会でも不在を確認済み（`post-teardown-state.md` で共有 ADB が検証前と
同じ状態に戻っていることも確認）。実施しなかった範囲は `e2e/SKIPPED.md`。
証跡の OCID・ネームスペースは `spikes/spike_m1/redact_evidence.py` で伏字化。

- `.venv/bin/pytest packages/api/tests` 381 passed
  （新規 `packages/api/tests/test_ops_adb.py` 38 件＝ゲートの否定側の単体テスト）
- `.venv/bin/ruff check packages/api ops spikes` クリーン
- `terraform init -backend=false && fmt -check && validate`（environments/app）成功

## IAM

**変更していない**（タスクの禁止事項）。既存の動的グループ／ポリシーのままで
シナリオ 2・3 が通ったため、権限不足による blocked は発生していない。

## review-1（FAIL: blocker 4 / major 3）への対応

| 指摘 | 対応 |
|---|---|
| B-001: 新規 `ops/_adb.py` が差分に含まれず、clean checkout では `ModuleNotFoundError` | 新規ファイルを `git add -N` して差分に載せた（コミットはしない） |
| B-002: `show_target()` は表示のみで、DDL 前に接続先を検証していない | `_adb.assert_target()` を新設。`.env` の `ADB_OCID` を API で解決し、`DB_NAME` のインスタンス固有トークンが当該 ADB の接続文字列に現れることまで照合。未設定・不一致は SystemExit（両スクリプトの DDL 前に必ず通す） |
| B-003: E2E 台帳が名前だけで所有権を判断し、teardown 後も失効しない | 台帳に「作った証拠」（ユーザーの作成時刻 / バケットの etag・作成時刻）を記録し、再照合できたものだけ再利用・削除。削除できたら台帳から落とす。シナリオ1は対象スキーマが**既存なら中止** |
| B-004: E2E の SQL 接続先が API 確認した ADB と同一とは限らない | ドライバも `ops/_adb` 経由で接続し `assert_target()` を通す。ウォレット展開先を `ADB_OCID` ごとに分離 |
| M-001: ORA-28007 を「現行値と同じ」と断定 | 実ログインで裏取りし、入れなければ中止（推測でパスワードを案内しない） |
| M-002: パスワード不明時に migrate を飛ばして exit 0 | 非ゼロ終了に変更。意図的に飛ばすなら `--skip-migrate` の明示を要求 |
| M-003: 片付け証跡が主張と不整合（bucket/profile/index の削除が無い） | シナリオ1→4→teardown を**通しで 1 回**流し直し、teardown は削除後に再照会して不在を確認・失敗があれば非ゼロ終了。台帳は完了後 `{}` |

## review-2（FAIL: blocker 1 / major 5 / minor 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: teardown が所有権照合より先にスキーマ内オブジェクトを削除する | 所有権照合を **最初に ADMIN 接続で**行い、一致しなければスキーマへ接続すらしない。照合は作成時刻に加えて**この run 固有の乱数マーカー表**（`RP01_RUN_MARKER`）。否定 E2E（`driver.py guard`）で「台帳が壊れていると exit 1・リソース無傷」を実機確認 |
| M-001: `call_timeout=0`（無期限）で片付けに到達できなくなる | 有限値へ（通常 600s・索引構築 900s） |
| M-002: `/tmp` の認証資材が既定権限で残る | ディレクトリ 0700・秘密ファイル 0600 で作成し、teardown で削除 |
| M-003: 所有証跡の記録が遅く、途中失敗で孤児が残る | 作成直後に記録し、シナリオの `finally` でも「存在するのに台帳に無いユーザー」を回収。バケット・索引も作成前後で記録 |
| M-004: 安全クリティカルな共通部に単体テストが無い | `packages/api/tests/test_ops_adb.py`（17 件）。ADB_OCID 未設定 / トークン不一致 / DB_NAME 形式不正 / RP 未付与 / ORA-28007 のログイン失敗 など**通ってはいけない経路**を網羅 |
| M-005: 証跡にウォレットパス（ADB_OCID 由来）が残る | パスの接尾辞を ADB_OCID の**ハッシュ**に変更し、ログ出力自体も `<WALLET_DIR>` に伏字化 |
| m-006: `JETUSE_APP` の RP 付与に証跡が無い | `post-teardown-state.md` に `DBA_TAB_PRIVS` の read-only 照会結果を追加 |

## review-4（FAIL: blocker 1 / major 2 / minor 1）への対応

review-3 は codex が異常終了（ERROR）。原因は E2E 証跡ディレクトリに `__pycache__/*.pyc`（バイナリ）が
できていて、レビュー入力が UTF-8 として壊れたこと。`__pycache__` を削除して再実行した。

| 指摘 | 対応 |
|---|---|
| B-001: `DROP USER` は台帳の名前だけで実行しており、読取専用ユーザーは作成時刻を一度も照合していない（TOCTOU） | `DROP USER` の直前に**両ユーザー分**作成時刻を、アプリスキーマはマーカーも再照合する。1 件でも不一致なら 1 件も DROP しない。否定 E2E に「読取専用ユーザーだけズレている」ケースを追加（開始時のゲートは通過するので、この再照合でしか止まらない） |
| M-001: RP 付与の確認が `OWNER` を限定しておらず、別スキーマの同名 grant を誤認しうる | 照合条件に `owner = 'ADMIN'` を追加＋単体テスト |
| M-002: teardown が失敗すると `/tmp` に認証資材が残る | `try/finally` で必ず片付け。ただし**作り直せない**検証スキーマのパスワードだけは残作業があるとき保持する（消すと残ったスキーマを片付けられなくなるため）。ウォレットは常に削除 |
| m-003: パスワードを `IDENTIFIED BY "..."` へ直接補間している | `_adb.assert_password()` で引用符・空白・改行・長さを拒否（拒否時は DDL を 1 本も出さない）＋境界テスト |

## review-5（FAIL: blocker 2 / major 3 / minor 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: `assert_target()` は「SQL 接続先＝ADB_OCID」しか見ておらず、承認済みテナンシ/コンパートメントかを見ていない | ADB のコンパートメントをルートまで辿り、`.env` の `COMPARTMENT_OCID` を経由することを **OCID で**確認する（名前は根拠にしない）。未設定・不一致は DDL 0 件で中止。同名の `jetuse/dev` 階層を持つ別テナンシを拒否する単体テストを追加 |
| B-002: E2E 側の認可も表示名とコンパートメント名だけ | ドライバは `_adb.assert_target()`（OCID 照合）を必ず通す。`assert_loop_adb()` の名前照合はその上乗せに位置づけ直した |
| M-001: app 既存＋パスワード未指定のとき、読取専用ユーザーを作った後に落ちて生成パスワードが失われる | **DDL 前にプリフライトで中止**（何も作らない）。生成パスワードの出力も migrate より前に移し、migrate 失敗でも失われないようにした |
| M-002: 台帳へ書く前にマーカー表を CREATE（暗黙コミット）しており、途中失敗で孤児になる | ユーザーの識別値を**先に**永続化 → マーカーは INSERT 成功後に追記。既存の空マーカー表からの回収も実装（ORA-00955 を許容） |
| M-003: 接続後の SQL 往復に有限タイムアウトが無く、別プロセスの ops へは driver の設定が伝わらない | `_adb.connect()` が `call_timeout` を設定（既定 600s・`ADB_CALL_TIMEOUT_MS` で上書き・0 以下は拒否）＝ops 本体にも効く |
| m-001: `settings.select_ai_credential` の既定変更に単体テストが無い | 既定が `OCI$RESOURCE_PRINCIPAL` であることと、env で旧名へ戻せることをテストで固定 |

## review-6（FAIL: blocker 4 / major 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: 承認済み `COMPARTMENT_OCID` の**子孫**まで許すため、dev 以外の ADB でも通る | **完全一致**に変更。ADB が子コンパートメントにある環境は `.env` の `ADB_COMPARTMENT_OCID` で明示する（`.env.example` に追加）。承認済みの子を拒否する単体テストを追加 |
| B-002: E2E 側も表示名とコンパートメント名だけを認可根拠にしている | 承認済みの根の直下から `dev` の OCID を引き、ADB のコンパートメントと**OCID 完全一致**を要求。その OCID を `ADB_COMPARTMENT_OCID` として ops サブプロセスへ渡し、両者を同じ値で照合 |
| B-003/B-004/B-002 の TOCTOU 系: 固定名の資源を事後照会で所有物として取り込みうる | **資源名を run 固有にした**（`rp01<乱数4>`）。衝突自体が起こらず、既存なら中止。バケット作成の 409（競合）・結果不確定は台帳へ入れない＝自動削除しない |
| M-001: query ユーザーだけ作って落ちると生成パスワードが失われる | 両パスワードを**全 DDL の前に**検証し、生成値も DDL 前に出力する。app 既存＋パスワード未指定＋migrate 要求はプリフライトで（何も作らずに）中止 |

## review-7（FAIL: blocker 3 / major 1 / minor 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: `ADB_COMPARTMENT_OCID` の上書きが承認済みの根と無関係でも通る | 上書き値が `COMPARTMENT_OCID` の配下にあることを OCID で遡って確認。別テナンシの値を上書きに指定した否定テストを追加 |
| B-002: 事前確認〜ops 実行の間に作られた同名ユーザーを所有物として取り込みうる | ops 実行の**直前に DB の現在時刻**を取り、それより前からあるユーザーは所有物にせず中止する（作成時刻の下限で「この run が作った」を担保） |
| B-003: app の `DROP ... CASCADE` の後、query は名前だけで DROP していた | **1 件ごとに DROP の直前**で作成時刻を再照合（アプリスキーマはマーカーも）。合わなければその 1 件を DROP しない |
| M-001: DROP の任意のエラーを握り潰し、再照会前に台帳から落としていた | 「既に無い」を表す ORA コードだけ成功扱いにし、それ以外は失敗として数える。台帳から落とすのは**不在を再照会で確認できたときだけ**（照会不能なら残す） |
| m-001: 片付け後の台帳が `{}` という記述が実物と違う | 「資源の項目はゼロで `run_tag` だけ残る」に修正 |

## review-8（FAIL: blocker 1 / major 2 / minor 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: 所有権取得を拒否した後、`finally` の再取得が下限なしで同じユーザーを取り込む | `finally` も 1 回目の ops 実行直前の DB 時刻を下限として渡す（拒否したものを後から拾わない） |
| M-001: DROP の任意のエラーを「既に無い」として握り潰し、再照会前に台帳から落としていた | **前ラウンドで書いたつもりの修正が当たっていなかった**（引用符の正規化でパターンが一致せず無音で失敗）。今回は編集の適用を確認済み。既知の not-found（ORA-20004 / ORA-00942）だけ成功扱いにし、他は失敗として数える。台帳から落とすのは不在を再照会できたときだけ |
| M-002: 生成パスワードをプリフライトより前に出しており、中止時に「設定していない値」を案内する | 出力をプリフライト後・DDL 前へ移した |
| m-001: ADB 表示名・DB 名・共有バケット名が検証コードに固定 | **未対応（residual）**。`spikes/spike_m1/common.py` と同じ「承認済み対象を名前で固定する fail-closed ガード」の踏襲で、.env 化すると「.env を差し替えれば別環境を触れる」ことになり弱くなる。判断は人間ゲートへ |

## review-9（FAIL: blocker 1 / minor 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: 事前確認〜ops 実行の間に同名ユーザーが作られると、明示パスワード指定の ops が既存ユーザーを ALTER しうる | `ops/setup-dev-schema.py` に **`--require-new`** を追加（既存があれば ALTER も CREATE もせず中止）。E2E の 1 回目はこれを付けて実行し、競合時に他人のスキーマへ触れない。2 回目は付けない（＝冪等性の確認そのもの）。単体テストを 2 件追加 |
| m-001: ADB 表示名・DB 名・共有バケット名が検証コードに固定 | **未対応（residual・人間ゲートへ）**。承認済み対象を名前で固定するのは `spikes/spike_m1/common.py` と同じ fail-closed ガードの踏襲で、.env 化すると「.env を差し替えれば別環境を触れる」ことになりゲートとして弱くなる |

## review-10（FAIL: blocker 1 / minor 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001: `--require-new` で setup が中止しても、`finally` の所有権回収が「作成時刻が下限以降」だけで取り込んでしまう | 所有権を取るのは **setup が exit 0 のときだけ**に限定（`created_by_us`）。1 回目は `--require-new` が通っている＝「実行前に存在せず、この実行が作った」が確定する。失敗時は台帳へ入れず「残っていないか人間が確認すること」と警告を出す |
| m-001: 環境依存値（ADB 表示名等）が検証コードに固定 | 前回と同じく **未対応（residual・人間ゲートへ）** |

## review-11 への対応（人間ゲートの判断: override せず修正）

**停止理由として書いた「blocker と major が互いに反対の要求」は誤りだった。** review-11 の
suggestion は両方とも同じ機構を提示していた:「`CREATE USER` の直後に、作り直しで変わる識別子
（`DBA_USERS.USER_ID`）を receipt として永続化し、台帳化のときと DROP 直前に照合する」。
＝「作った瞬間に、作り直しを検出できる形で記録する」1 つの機構で両方が閉じる。

| 指摘 | 対応 |
|---|---|
| B-001: 作成を証明できないユーザーを取り込みうる | `ops/setup-dev-schema.py` に `--receipt <path>` を追加し、**CREATE 直後に** `USER_ID` / 作成時刻 / この実行が作ったかを書き出す。ドライバは「いま在るユーザー」を推測で取り込まず receipt だけを根拠にし、台帳化時・DROP 直前・各 DROP の直前に `USER_ID` を照合する |
| M-001: setup が途中失敗すると作成物が孤児になる | receipt は CREATE 直後にあるので、権限付与・RP 有効化・migration のどこで失敗しても台帳化でき、teardown で消せる。`created_by_us` のようなプロセス全体の成否ゲートは廃止 |
| m-001: 環境依存値の直書き | **人間ゲートで「意図的な residual として受容」と決定**（`.env` 化すると差し替えで別環境を触れることになり、安全装置として弱くなるため）。理由を `docs/verification/RP-01.md` に明記 |

追加した競合テスト:
- 実機（`guard.md`）: `USER_ID` を食い違わせた 2 ケース＝「同じ秒内に DROP → 同名で再作成」相当。
  作成時刻では区別できないが `USER_ID` で検出し、**ユーザーもマーカーも無傷のまま非ゼロ終了**する。
- 単体（`test_ops_adb.py`）: CREATE 直後に receipt が出る / **CREATE 後の GRANT 失敗を注入しても
  receipt は残る** / 実行前から在ったユーザーは `created_by_this_run=false` になる / `--receipt` 無しでも動く。

## review-12（FAIL: blocker 2 / major 2）への対応

**成果物（`ops/`）への指摘が 3 件出たため、人間が定めた停止条件に従って修正した。**

| 指摘 | 対応 |
|---|---|
| B-001 `ops/setup-dev-schema.py`: receipt の `USER_ID` が CREATE の結果と結び付かず、作成後に名前で引き直している | `CREATE USER` と識別子取得を **1 本の PL/SQL ブロック**（`EXECUTE IMMEDIATE` + `SELECT ... INTO`）にまとめ、OUT バインドで受け取る。クライアント側の「作ってから読む」窓を無くした |
| B-002 `e2e/driver.py`: バケットの所有証跡を作成後の再照会で取っている | `create_bucket` の**応答そのもの**（`etag` / `time_created`）を記録。応答に etag が無ければ台帳へ入れず中止 |
| M-001 `ops/setup-dev-schema.py`: receipt の出力先を検証せず、CREATE の後で書き込むため証跡が残らない場合がある | `_assert_receipt_writable()` を **DDL の前**に実行（親ディレクトリの有無・書き込み可否・既存ファイルが JSON 配列か）。単体テスト 3 件追加 |
| M-002 `ops/setup-dev-schema.py`: `--require-new` が 2 ユーザーを一組で事前確認しておらず、部分作成を残しうる | `--require-new` 時は **app と query をまとめて先に照会**し、どちらかが在れば何も作らずに中止（CLI の契約どおり） |

## review-13（FAIL: blocker 2 / major 1）への対応

| 指摘 | 対応 |
|---|---|
| B-001 `ops/`: `CREATE USER` の暗黙コミット〜`SELECT` の窓は PL/SQL にまとめても残る | **原理的に閉じない**（識別子はオブジェクトができた後にしか読めず、DBA 権限の別セッションがその窓で作り直せば、クライアント側のどんな receipt 方式でも同じ）。クライアント側の窓は PL/SQL 化で除去済み。緩和 3 点（run 固有名 / `--require-new` / 破壊操作ごとの再照合）とあわせて `docs/verification/RP-01.md`「既知の限界」とコード docstring に明記した |
| B-002 `e2e/driver.py`: 作成応答を台帳化した後、書き込み前に再確認していない | `put_object` の直前にバケットの同一性（etag / 作成時刻）を再照合し、違えば**書き込まずに中止** |
| M-001 `ops/`: receipt の事前検証が配列であることしか見ていない / open 成功は write 成功を保証しない | 各要素が dict であることまで検証し、事前に**実際の書き込みを試す**（新規なら書いて消す）。`_write_receipt` 側も dict 以外の要素を無視する |

## review-14: PASS（完了ゲート到達）

blocker 0 / E2E adequacy=sufficient。loop-protocol の停止規律に従い、PASS の下に残る
非 blocker は**修正せず residual として列挙**する（磨き込みの反復に入らない）。

| sev | file:line | 指摘 | 扱い |
|---|---|---|---|
| major | `ops/setup-dev-schema.py:119` | receipt を `write_text` で切り詰めるため、ENOSPC やプロセス停止で既存 receipt が壊れうる | residual（原子的書き込み＝一時ファイル + rename にすべき。後続トリアージ） |
| major | `runs/<run-id>/e2e/driver.py:284` | 所有権台帳も直接上書き。更新中に停止すると JSON が壊れ teardown 不能になりうる | residual（同上。検証スクリプト側） |
| minor | `ops/_adb.py:230` | `verify_login()` が全 `DatabaseError` を「パスワード違い」と診断する（通信障害等も同じ扱い） | residual |
| minor | `runs/<run-id>/e2e/driver.py:311` | `read_marker()` が全 `DatabaseError` を「マーカー無し」と扱う | residual |

受容済み residual（人間ゲートで決定・2026-07-29）: `review-11 m-001` 検証コードへの環境依存値の直書き。
既知の限界（原理的に閉じない）: `CREATE USER` の暗黙コミット〜識別子取得の窓（`docs/verification/RP-01.md`）。

## 人間ゲート（未実施）

- コミット / PR / push（未承認のため未実施）
- 配備済み dev アプリスタックへの `terraform apply` と `/api/health` の `dbchat` 確認（SKIPPED.md）

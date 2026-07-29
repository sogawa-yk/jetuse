# STATE — SPIKE-M1（RAG メタデータの実現方式スパイク）

- task: SPIKE-M1
- run_id: 2026-07-28T1848_SPIKE-M1
- branch: feat/SPIKE-M1（base: main。共有物のため dev ではなく main 起点）
- area: api
- review_verdict: **PASS**（review-8。blocker 0 / major 7 / minor 1・E2E adequacy=sufficient）
- last_review_ref: runs/2026-07-28T1848_SPIKE-M1/reviews/review-8.json
- updated_at: 2026-07-28

## 検証環境

共有 loop ADB `jetuse-loop-adb`（ap-osaka-1 / Oracle AI Database 26ai 23.26.3.1.0）に
スキーマ `JETUSE_SPIKE_M1` を作って隔離（ADB は増やしていない）。
検証用 OCI リソースは `jetuse-spike-m1`（バケット）/ `jetuse-spike-m1-vs`（Vector Store）/
`jetuse-spike-m1-project`（GenAI プロジェクト）。片付けは `spikes/spike_m1/teardown.py --yes`。

> 人間ゲート事項: 共有 loop ADB の **ADMIN パスワードをリセットした**（接続情報がこの worktree に
> 無く、②③ の実機検証が不可能だったため。承認を得て実施）。新パスワードは gitignore 済みの
> `.env` にのみ置いてある。他の利用者と共有する場合は同期が必要。

## 完了したこと

- 方式③（ADB 自前索引 / DBMS_VECTOR_CHAIN）: **PASS**。版フィルタ付きベクタ検索が 1 本の SQL で成立し、
  フィルタ無しでは旧版 c08/c09 がヒット、`current_version='Y'` では旧版 0 件（対照成立）。
  出典は列 / JSON として構造化返却。DB 内埋め込みも動作（1024 次元）。
- 方式②（Select AI 索引）: 任意キーは `ORA-20048` で**拒否**。`$VECTAB` は固定 6 キーのみ。
  列追加は可能だが**索引リフレッシュで新規行が NULL**（実測）。
- 方式①（OCI Vector Store + file_search）: **属性付与もフィルタ検索も可**だった（当初想定と逆）。
  限界は「属性がファイル単位」「キー16/値512/入れ子不可」「`in` 不可」「未定義キーが静かに 0 件」。
- レイテンシ実測（各 5 回・中央値／**3 方式が同じ chunk_id 集合 c01〜c10 を見ていることを計測前に実測確認**）:
  ③ 16.7 ms（SQLのみ・別途埋め込み 84.5 ms）/ 192.7 ms（DB内埋め込み込み）、② 69.3 ms、
  ① 121.5 ms（検索のみ）。生成込みは ① 4324 ms / ② 3276 ms。
  実行ごとの揺れが大きく **レイテンシは方式選択の決め手にならない**（同じ桁）。
- 成果物: `docs/verification/SPIKE-M1.md` / `docs/comparison/rag-metadata-backends.md` /
  `docs/decisions/ADR-0019-rag-metadata-backend.md`（**proposed**）/ `docs/tips.md` 追記 /
  `spikes/spike_m1/`。E2E 証跡は `runs/<run-id>/e2e/`（scenario-1〜3 + SKIPPED.md + 生ログ）。
- `.venv/bin/pytest packages/api/tests` 325 passed / `.venv/bin/ruff check packages/api spikes/` クリーン。

## review-1（FAIL: blocker1 / major4 / minor1）への対応

| 指摘 | 対応 |
|---|---|
| blocker: 接続先を検証せず DDL を打つ | `common.assert_target()` を追加し、DB_NAME=JETUSELOOP2・region=ap-osaka-1 を実際に問い合わせて確認してから DDL を打つ（fail-closed）。setup_schema / teardown の両方で通す |
| major: 索引作成が全滅しても握り潰す | `build_index()` は HNSW/IVF 双方失敗で `RuntimeError`。作成できた索引種別を判定行に出す |
| major: `load_env()` の setdefault と戻り値がずれる | 戻り値を実効値（`os.environ`）に統一 |
| major: `call_timeout = 0`（無期限） | `common.CALL_TIMEOUT_MS`（既定 600 秒・env で上書き可）を接続時に設定 |
| major: teardown.py が無いのに案内していた | `spikes/spike_m1/teardown.py` を追加（既定 dry-run・`--yes` で実削除） |
| minor: 「実プラン」の記述が不正確 | EXPLAIN PLAN は推定計画であること、実績プランは V$SESSION 権限が無く取れないことを docstring とレポートに明記 |

## review-3（FAIL: blocker5 / major8）への対応

| 指摘 | 対応 |
|---|---|
| blocker×3: 名前一致だけで既存 OCI リソース（Vector Store / バケット / GenAI プロジェクト）を再利用・削除 | 作成時に `created-resources.json` へ ID を記録する台帳を導入。再利用も削除も**台帳にある ID だけ**。台帳に無い同名は中止 |
| blocker: `connect_spike()` がガードを通らない | `connect_admin` / `connect_spike` の両方で `assert_target()` を通す |
| blocker: 既存ユーザーへ無条件に ALTER/GRANT/ACL | 既存かつ台帳に無ければ中止。ADB は `<prefix>_<db_name>` を返すため判定を末尾一致に修正 |
| major: 「本文が ADB から出ない」が実装と矛盾 | **事実誤り。訂正済み**。`UTL_TO_EMBEDDING` は本文を OCI GenAI の `embedText` へ送る。比較表・ADR・検証レポートの該当箇所を「アプリ層を経由しない／テナント外に出ないわけではない」に修正 |
| major: `CALL_TIMEOUT_MS` が import 時評価・0 を許す | `call_timeout_ms()` を接続時に解決し、0 以下は拒否 |
| major: ① の取り込み待ちがストア全体の件数 | 今回登録した file_id だけを待ち、失敗・タイムアウトは例外 |
| major: ②の索引構築タイムアウト / 常に exit(0) | タイムアウトを例外化。`sys.exit(0)` を撤去 |
| major: refresh チェックが失敗を握り潰す | STOP/RUN/START の失敗を再送出。プローブ用オブジェクトは**未使用の名前を選ぶ**（既存名だと「新規行なし＝欠損なし」と誤読する。実際に一度誤読した） |
| major: ③ の PASS 条件が緩い（B が 0 件でも PASS） | 対照成立・B が 5 件・全件現行版・C も非空、まで確認 |
| major: fingerprint をログ出力 | 出力を削除 |
| major: teardown が DB 失敗で OCI 片付けに到達しない | 各段を独立実行し、失敗を集約して非ゼロ終了 |
| E2E: 否定シナリオが無い / teardown 未実施 | `guard_checks.py`（G1〜G3）を追加し scenario-4 として証跡化。`teardown.py --yes` を実行し、残骸ゼロを OCI/ADB へ問い合わせて確認（`teardown.log` / `teardown-verify.log`） |

## review-4（FAIL: blocker5 / major5 / minor1）への対応

| 指摘 | 対応 |
|---|---|
| blocker: COMPARTMENT_OCID が承認済みか未検証のままリソース作成 | `assert_compartment()` を追加。OCID を **API で名前解決**して親子（`jetuse`/`dev`）まで照合。バケット作成・プロジェクト作成の前に必ず通す。**これで `.env` が旧レイアウトの `jetuse-dev` を指していた誤りが実際に検出された**（loop ADB がいるのは `jetuse/dev`）。`.env` を修正して全証跡を取り直した |
| blocker: 書き換え可能な purpose メタデータを所有の根拠にしていた | 削除。**台帳（作成時に記録した ID）だけ**を所有の根拠にする |
| blocker: teardown が `_clients()` 経由でプロジェクトを新規作成しうる | `_clients(allow_create=False)`。片付け経路では作成を禁止し、無ければ中止 |
| blocker: guard_checks G2 がスキーマ不在時に本物を作ってしまう | 実行前に存在確認し、無ければ SKIP（理由を出力） |
| blocker: DB_NAME 末尾一致だけでは別 ADB を弾けない／台帳 ID がスキーマ名のみ | `.env` の ADB_OCID を Database API で解決し **表示名 `jetuse-loop-adb` とコンパートメント**まで照合。台帳の ID も `<DB_NAME>:<SCHEMA>` に変更 |
| major: `list_objects` の全例外を「既に無い」扱い | `ServiceError.status == 404` のみ正常扱い。他は再送出 |
| major: RUN 失敗で START に到達せずパイプライン停止のまま／プローブ残留 | `try/finally` で必ず START。プローブのオブジェクトと `$VECTAB` の行を後始末 |
| major: 計測条件が揃っていない（① 11 ファイル / ② 12 行） | 計測前に 3 方式の件数を実測し、10 件で揃わなければ**計測を中止**する。全証跡を取り直し 10/10/10 で再計測 |
| major: tips の「本文を DB の外へ出さずに」が他文書と矛盾 | 訂正（embedText へ送られる旨を明記） |
| minor: 比較表の「5.2 秒」が実測と不一致 | 実測値に更新 |
| major: redact がエンドポイント実値を残す | **対応しない（残差）**。対象は 地域推論エンドポイント 等の**公開されている地域サービスエンドポイント**で、CLAUDE.md 本文にも記載がある。伏せると「どの API を叩いたか」という検証の核が読めなくなるため残す。テナント固有値（OCID・ネームスペース）は伏せ済み |

## review-5（FAIL: blocker6 / major6 / minor1）への対応

| 指摘 | 対応 |
|---|---|
| blocker: 単独実行の各スクリプトが所有ゲートを迂回（method_c の DROP TABLE / refresh の STOP・put_object / limits のファイル追加） | `require_owned_schema` / `require_owned_bucket` / `require_owned_store` を common に集約し、**各スクリプトの冒頭で必ず呼ぶ**ようにした（ゲートを経路ごとに書かない） |
| blocker: コンパートメント判定が名前だけでテナンシを見ていない | ルートコンパートメントまで辿り、`~/.oci/config` の tenancy と一致することを確認 |
| blocker: 台帳が teardown 後も失効せず、別人が同名を作り直すと誤認 | 削除成功時に `forget_created()` で台帳から落とす。teardown 後の台帳は空（証跡で確認） |
| blocker: `PROJECT_OCID` が env にあると無検証で再利用 | env 経由でも コンパートメント・表示名・台帳の 3 点を検証 |
| major: 台帳が e2e/ にあり、伏字化スクリプトが ID を壊す（片付け不能になる） | 台帳を gitignore 済みの `.spike-m1-registry.json` へ移動。証跡には**名前だけ**の写し `created-resources-names.json` を出す |
| major: 同一データセットの確認が件数だけ | 各方式から chunk_id を取り出し、集合が `c01〜c10` と一致するかで判定（不一致なら計測中止） |
| major: プローブの後始末が正常系のみ／teardown の一覧が非ページング・削除後未確認 | プローブ削除を確実化。バケット/ストアの一覧をページングし、削除後に再照会して消滅を確認 |
| major: 必須呼び出しが失敗しても exit 0 | ①の Responses と ②の GENERATE の失敗で非ゼロ終了 |
| major: teardown にエンドポイント実値をハードコード | リージョンから組み立てる形に変更 |
| minor: limits の取り込み未完了でも判定を続行 | `completed` 以外なら判定不能として中止 |

## review-6（FAIL: blocker1 / major9 / minor1）への対応

| 指摘 | 対応 |
|---|---|
| **blocker**: SQL 接続先と ADB_OCID が同一 ADB だと結びついていない（DB 名が同じ別 ADB を通す） | ADB API の接続文字列に **DB_NAME のインスタンス固有トークン**（`G912…`）が含まれることを照合し、SQL セッションと ADB_OCID を同一 ADB に固定。実機で通過を確認 |
| major: `sdk_signer_args(region)` は config_file モードで region を無視する | `common.client_args()` を追加し、全 OCI クライアントで region を明示。`~/.oci/config` が別リージョンでも想定外の場所に作らない |
| major: 成果物にエンドポイント実値 | ドキュメントの URL を `<region>` プレースホルダ表記に変更（検証リージョンは本文で明記） |
| major: ① が件数だけ見て再アップロードを省略／API 成功だけで PASS | 既存ストアの chunk_id 集合が基準セットと一致するかを検証。検索も**結果が空でない**・**版フィルタ時に旧版が混ざらない**まで判定 |
| major: limits / refresh のプローブ後始末が正常系のみ | いずれも `try/finally` へ移動。後始末に失敗したら非ゼロ終了 |
| major: refresh が表全体の NULL 件数で判定 | **今回のプローブ行だけ**の `current_version` を見る |
| major: latency が set 化で重複を消す・1 ページ目しか見ない | 実レコード数と異なる chunk_id 数の両方を出し、重複も不一致として弾く。一覧はページング |
| major: teardown の files 一覧が非ページング | ページング対応 |
| major: guard_checks が固定パスのファイルを上書き・削除 | 一時ディレクトリへ書く |
| minor: 比較資料の本文に旧計測値が残存 | 表・本文・Tips をすべて最新実測へ統一 |

## review-7（FAIL: blocker2 / major8 / minor2）への対応

| 指摘 | 対応 |
|---|---|
| **blocker×2**: 台帳の ID が再利用可能な名前（`DB_NAME:SCHEMA` / `namespace/bucket`）で、削除後に他人が同名を作ると stale ledger が一致して他人のリソースを変更・削除しうる | スキーマは **作成時刻**（`all_users.created`）を、バケットは **bucket OCID** を台帳 ID に含めた。作り直されたら別物として弾く |
| major: GenAI プロジェクト削除が受付だけで未確認／台帳 ID が一覧に無くても成功 | 削除後に `lifecycle_state` を再取得して DELETING/DELETED を確認。台帳にあって一覧に無いものは失敗として報告 |
| major: ストア切り離しと Files 本体削除が同一 tuple | 別ステップに分離（本体削除だけ失敗しても孤立ファイルを見失わない） |
| major: ① が 1 件成功で「属性対応」／`?`（属性欠損）を成功扱い／search 失敗が終了コードに出ない | 全件成功で初めて対応と判定。フィルタ時は `N` と `?` の両方を不合格に。search API の失敗も終了コードへ |
| major: limits の判定が緩い／try 外で残留しうる | ヒット 0 件・属性種類 ≠1 を失敗に。登録も try/finally の中へ |
| major: ② が `$VECTAB` 1 行で構築完了と判定 | 基準セット件数（10）に達するまで待つ |
| major: refresh の想定外・後始末失敗が exit 0 | いずれも例外にして非ゼロ終了 |
| major: 伏字化が OCID と namespace だけ | Vector Store ID・file ID・ADB インスタンストークン・地域エンドポイント（`<region>` 化）も伏せる |
| major: 比較表で VPD を確定事実として記載（SKIPPED では未実証） | 「原理的には併用できるが**本スパイクでは未実証**」に修正。ADR にも同注記 |
| minor: ADR 本文の旧数値／teardown docstring の台帳パス誤り | 実測値へ統一。docstring を `.spike-m1-registry.json` に修正 |

## 片付け結果（実行済み）

`teardown.py --yes` 実行後の実機確認（`e2e/teardown-verify.log`）:
**台帳の残り = 空**（すべて削除され台帳からも失効）。SPIKE-M1 のバケット なし /
GenAI プロジェクト なし / `JETUSE_SPIKE%` スキーマ なし。バケット（404）・Vector Store・
GenAI プロジェクト（DELETING）・スキーマの 4 つすべてを削除後の再照会で確認
（`teardown.log` の「確認: …」4 行）。
他タスクの `jetuse-spike-sp202-rag` / `jetuse-spike-sp303-e2e` は不可触のまま。
共有 ADB の他 `JETUSE_` スキーマ 7 件は無傷。

## 自損事故（正直に記録）

`guard_checks.py` の G3 初版が、**DB 側だけ台帳ゲートの無かった** `teardown.py --yes` を
空台帳で実行し、検証スキーマ `JETUSE_SPIKE_M1` を実際に削除した。共有 ADB の他スキーマ・
他リソースへの波及は無し。DB 側にも台帳ゲートを入れ、スキーマを作り直して ①②③ の証跡を
すべて取り直した。詳細 `e2e/scenario-4.md`、Tips にも追記済み。

## PASS 後に残った非 blocker（residual・後続トリアージ）

review-8 は blocker 0 で PASS。以下は**修正せず残した**助言（loop-protocol 手順 5.5「PASS 後は
非 blocker を追わない」）。いずれも検証スクリプトの堅牢性であって、**検証結果そのものには影響しない**。
スクリプトは調査用の使い捨てで、片付けは実機で完了を確認済み。

| severity | 箇所 | 内容 |
|---|---|---|
| major | `spikes/spike_m1/redact_evidence.py:49` | 伏字化が引数で渡したサブツリー限定。`reviews/` は対象外 |
| major | `spikes/spike_m1/method_a_vector_store.py:75` | GenAI プロジェクト作成直後に ACTIVE を待たずに使う |
| major | `spikes/spike_m1/method_a_limits.py:164` | `multi_id` が正常終了後にしか設定されず、途中失敗で検証ファイルが残りうる |
| major | `spikes/spike_m1/method_a_limits.py:102` | 属性の Y→N→Y 復元が失敗しても終了コードに出ない |
| major | `spikes/spike_m1/method_b_refresh_check.py:54` | プローブの `put_object` が try/finally の外 |
| major | `spikes/spike_m1/teardown.py:196` | Vector Store の削除後確認だけ先頭ページ限定 |
| major | `STATE.md:82` | 「エンドポイント実値」の扱い（→ 本ターンで `<region>` 化して解消済み） |
| minor | `docs/decisions/ADR-0019…:30` | 本文の旧計測値（→ 本ターンで解消済み） |

## 人間ゲートの結果（2026-07-29）

- **ADR-0019 承認済み**。承認時にユーザーが決定内容を拡張:
  「どちらか一方でなく **選べる 2 バックエンド**（手軽さ＝マネージド Vector Store /
  高機能＝Oracle AI Database）」「**Oracle AI Database は Agent Memory の格納先としても使う**」
  「**高機能側で何が増えるかが API だけでなくフロントからも分かること**」。
  → ADR を `Accepted` に更新し、決定・却下理由・影響・実装タスクを書き換えた。
  比較ドキュメントにも「JetUse としての採用」節を追加。
- 起票した実装タスク: `tasks/RAGM-PROGRESS.md`（RAGM-01 / RAGM-02 / RAGM-03 / MEM-01）。
  SPIKE-M1 の宿題 4 件（ops パーサ修正・スケール検証・VPD 実証・Chicago）も同キューに記載。

## 未完（人間ゲートのみ）

- [ ] push / PR（コミットは承認済み）

## 後続タスクとして起票が必要（本タスクの非ゴール）

- `ops/setup-select-ai.py` / `ops/setup-dev-schema.py` の `~/.oci/config` パーサ修正
  （全プロファイルを 1 dict に潰すため、複数プロファイル環境で credential が別テナンシになり
  DBMS_CLOUD の全呼び出しが `ORA-20404` になる。本スパイクは `spikes/spike_m1/common.py:oci_api_key()`
  で回避しているだけで、`ops/` は未修正）
- ③ の大規模データでの索引利用・再現率の検証（10 行ではオプティマイザが索引を使わない）
- Chicago リージョンでの ①③ 再確認

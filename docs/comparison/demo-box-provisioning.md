# 比較: デモの「箱」のプロビジョニング方式（SP2 / specs/18 §3）

日付: 2026-07-06。対象: specs/18-sp2-demo-tenancy.md §3 の決定根拠。
デモごとのデータ分離（specs/17 §5「箱の分け方」）を **(A) どの実体で分けるか** と
**(B) いつ作るか**、**(C) どう消すか** の 3 軸で比較する。

## 軸A: DB の箱 — 物理スキーマ vs 論理名前空間

| 観点 | A1: 物理スキーマ（デモごとに `CREATE USER DEMO_<id>`） | A2: 論理名前空間（datasets 機構流用）**← 採用** |
|---|---|---|
| 分離の実体 | Oracle ユーザー = スキーマ。DB 権限で分離 | `JETUSE_APP` 内テーブル + 登録簿 `JETUSE_DATASETS` の exact owner キー分離 + デモ専用 Select AI プロファイル（**demo の命名は完全 sha1 由来** — specs/18 §3.2。既存 user 資産のみ 8/16 桁互換） + VPD 行レベル境界 |
| 必要権限 | **実行時に ADMIN 相当（CREATE USER / GRANT / QUOTA / ACL）**。アプリ資格情報の常時昇格が必要（bootstrap.py は起動時のみ ADMIN を使う設計） | **ADMIN の常用は不要だが「承認済みの限定 EXECUTE 権限」は実行時に必要**（specs/18 §3.2.1・§4.3）: JETUSE_APP への DBMS_RLS 実行権と **DBMS_LOCK**（Oracle 推奨に沿い最小機能の cover package 経由での付与を第一案とする）、JETUSE_QUERY への setter パッケージ EXECUTE。初回付与は人間承認（SP2-02） |
| 実装コスト | 新規: ユーザー作成/削除・接続プール切替（スキーマごとに接続 or `ALTER SESSION SET CURRENT_SCHEMA`）・ACL/QUOTA 管理。**加えて右列の後始末・リース・上限は物理スキーマでも同等に必要**（箱の実体が DB 以外にもあるため） | 呼び出しは owner 引数に `ctx.namespace` を渡す形で既存機構を流用。ただし**タダではない**（specs/18 §3 の設計審査で確定した追加部品）: 排他リース（DBMS_LOCK）・registry-first 化（state + 回収）・VPD + bootstrap セットアップ（初回は人間承認）・完全ハッシュ命名・全 RAG バックエンドの後始末・delete_dataset の改修・箱あたり上限 |
| NL2SQL 経路 | プロファイルの object_list をスキーマ横断で組む改修が必要 | 既存のまま（デモ専用プロファイル `JETUSE_DS_<tag>` が自動で箱の表だけを見る） |
| 分離強度 | 強（DB 権限境界） | **強（VPD = DB 側の行レベル境界を正とする** — specs/18 §4.3。アプリ層の識別子走査は UX 補助）。動的 SQL 経由でもポリシーが適用される |
| 後始末 | `DROP USER ... CASCADE` 一発（利点） | 登録簿行の列挙 DROP（`delete_dataset` 流用・冪等） |
| ADB 上の制約 | ユーザー数はスキーマ管理・監査対象が増える。Autonomous DB の ADMIN 操作をアプリ実行時に常用する構成は反パターン | 表数が増えるのみ（デモ数 × データセット数。SA 個人利用規模で問題にならない） |

**判定: A2（論理名前空間）**。A1 の利点（DB 権限境界・DROP USER 一発）に対し、
実行時 ADMIN 昇格というセキュリティ後退と接続プール改修が釣り合わない。
specs/17 §5 の「別スキーマ `demo_<id>`」は A2 の論理名前空間として実装する
（同 §5 が流用先に指名する「サンプルスキーマ実体化の仕組み」の実体が datasets 機構であるため）。

## 軸B: 箱を作るタイミング — eager vs lazy

実測値（SPIKE-03 / SP1-03 / datasets.py 実装値）:

| 項目 | 実測・制約 |
|---|---|
| vector store 作成 + DP 伝播 | **40〜60 秒**（SP1-03 実測: 新規デモの初回 upload 完走 44 秒。有界リトライ実装済み） |
| vector store のテナンシ上限 | **completed store ≤ 10 / テナンシ**（SP1-03 で LimitExceeded 実測。ap-osaka-1） |
| datasets 表作成 + 投入 | 秒オーダー（CREATE TABLE + executemany） |
| Select AI プロファイル warmup | 最大 45 秒（datasets.py WARMUP_TIMEOUT_S。投入時のみ） |

| 観点 | B1: eager 同期（POST 内で箱を全部作る） | B2: eager 非同期（provisioning → ready） | B3: lazy（初回使用時）**← 採用** |
|---|---|---|---|
| POST /api/demos の応答 | **40〜60 秒 + warmup**（SSE でもないルートで非現実的） | 即時（ただし ready まで使えない） | **即時・即 ready** |
| vector store 枠の消費 | デモ数ぶん即消費 — **上限 10 で「RAG を使わないデモ」が枠を殺す** | 同左 | **RAG を実際に使うデモだけ消費** |
| 追加部品 | なし（ただし作成物の後始末・競合設計は同様に必要） | ジョブ実行基盤 or BackgroundTasks + 失敗リカバリ | **生成側の追加部品はなし**（ensure_store / create_dataset が既に lazy — SP1-03）。ライフサイクル全体では排他リース・registry-first 等が必要（specs/18 §3.2。これは B1/B2 でも同等に必要な部品） |
| 初回使用の体感 | 速い（作成済み） | 速い | RAG 初回 upload のみ +40〜60 秒（SP1-03 で有界リトライにより自動吸収を実機確認） |
| status 列の意味 | provisioning/ready/failed が本質 | 同左 | SP2 では ready/deleting のみ使用（provisioning/failed は SP3 のデータ生成用に予約） |

**判定: B3（lazy）**。決め手は vector store のテナンシ上限 10。eager は「箱=枠」の等式を強制し
10 デモで頭打ちになる。lazy は既存実装（ensure_store・create_dataset）の流用で追加コストゼロ、
初回 upload の遅延も SP1-03 で自動吸収を実証済み。

## 軸C: 削除の後始末 — 同期 vs 非同期

| 観点 | C1: 同期（DELETE リクエスト内）**← 採用** | C2: 非同期（worker / キュー） | C3: FastAPI BackgroundTasks |
|---|---|---|---|
| 所要時間 | **「1 回の要求で必ず完走」ではなく「各ステップ/チャンクが有界で、進捗を commit しながら再 DELETE で収束」**（specs/18 §3.2 — files/datasets は設定上限、会話/messages はチャンク削除）。上限フル時の実測を SP2-02 E2E の受け入れ条件にする | 同じ処理を別トラックで | 同左 |
| 追加部品 | なし | **常駐 worker（現行構成に存在しない）** | なし（プロセス内） |
| 失敗時の契約 | 503 → 同じ DELETE を再実行（各ステップ冪等で収束） | ジョブの死活監視・再試行制御が別途必要 | プロセス再起動でジョブ消失 → 結局「再 DELETE で収束」設計が必要 |
| 適用規模 | SA 個人単位のデモ数（〜数十）に十分 | 大規模テナント向け | 中間 |

**判定: C1（同期）**。冪等ステップ（specs/18 §3.2）はそのまま流用できるため、削除の実測が
specs/18 §3.1 の受け入れ閾値（配備経路の実効タイムアウト実測の 1/2）を超えたら
C3（BackgroundTasks）へ移行する余地を残す。ただし C3 は
**API 契約の変更を伴う**（202 応答 + 完了確認手段の追加。`deleting` は存在秘匿 404 のため
ポーリングには使えない — specs/18 §3.3）。C2 は規模に対して過剰。

## 採用構成のまとめ

- **A2 + B3 + C1**: 箱 = 論理名前空間（`demo_<uuid>` を既存機構の owner キーに差す）、
  生成 = lazy（POST は INSERT のみ）、削除 = 同期・順序付き・冪等（specs/18 §3.2）。
- プリセールス観点の含意: 「デモ 1 個あたりの追加インフラはゼロ。使った分（RAG を使うデモ）だけ
  vector store 枠を消費し、削除で枠が返る」— テナンシ上限 10 の下で最大数のデモを併存できる構成。

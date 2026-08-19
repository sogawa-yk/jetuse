# 開発者ごとのデプロイ済みE2E環境

複数人で開発し、各自が自分のブランチを実機デプロイしてE2Eテストするための仕組み。
高価な常設リソース(ADB/Identity Domain/VCN/OCIR等)は**共有**し、開発者ごとに分けるのは
**アプリ層(Container Instance + API Gateway + SPAバケット)と専用DBスキーマだけ**。
1人あたり追加コストは実質 Container Instance のみ(~$20-30/月、未使用時は停止/破棄可)。

> 設計の背景と検討経緯は計画(承認済み)に準拠。アプリのユーザーデータは元々 `owner_sub` で
> 分離されており、衝突するのは*デプロイ/コンピュート層*。そこだけを開発者ごとに分ける。

## 構成

```
共有(environments/dev が作成・正本):
  VCN/サブネット/NSG ・ ADB(jetuse-dev-adb) ・ Identity Domain ・ OCIR ・
  Gen AI Project ・ SemanticStore(SH) ・ wallet/app-data/speech バケット
        │ terraform_remote_state(local, ../dev/terraform.tfstate)で参照
        ▼
開発者ごと(environments/app, prefix jetuse-<dev>, state は <dev>.tfstate で分離):
  Container Instance(全 /api を自分のCIで処理) ・ API Gateway+deployment(専用NSGでIP制限可) ・
  SPAバケット+PAR ・ ADB上の専用スキーマ JETUSE_<DEV>(+読取専用 JETUSE_<DEV>_Q)
```

- per-dev ゲートウェイは `functions_routes={}` で**全 `/api` を本人CIへ**ルート(Functionsは共有・dev環境では不使用)。
- 認証は既定 `AUTH_REQUIRED=false`。OIDCリダイレクトURIをゲートウェイ毎に登録する手間を避け、
  分離は専用スキーマで担保する。公開GWのため `apigw_allow_cidr` で社内/VPNのIPに絞ることを推奨。
- DBスキーマは `settings.adb_user`/`adb_query_user`(環境変数 `ADB_USER`/`ADB_QUERY_USER`)で切替。
  既定は共有 `JETUSE_APP`/`JETUSE_QUERY`。
- DBの中で `DBMS_CLOUD`/`DBMS_CLOUD_AI` が使う資格情報は **`OCI$RESOURCE_PRINCIPAL`**(ADB自身の身分)。
  `ops/setup-dev-schema.py` が `ENABLE_RESOURCE_PRINCIPAL` を適用し、`environments/app` が
  `SELECT_AI_CREDENTIAL` を注入する。開発者のAPIキーをDBへ焼き込む経路は廃止(ADR-0021)。

## 前提(一度だけ・全体)

1. 共有 `environments/dev` を `terraform apply`(本対応で**出力を追加**したため、リソース変更0で
   stateに新出力を反映する必要がある)。
2. OCIRログイン済み、`.env` に `OS_NAMESPACE`/`COMPARTMENT_OCID` 等。
   `ops/setup-dev-schema.py` / `ops/setup-select-ai.py` は **`.env` の `ADB_OCID` と `COMPARTMENT_OCID` が必須**
   （DDL の前に「SQL の接続先がその ADB と同一か」を API で照合する fail-closed ゲートに使う。
   未設定・不一致なら何も変更せず中止する）。接続先は `ADB_DSN` / `ADB_WALLET_DIR` /
   `ADB_WALLET_PASSWORD` で上書きできる。

## 開発者の追加(1人につき一度)

```bash
# 1) 専用スキーマ作成 + 権限 + リソースプリンシパル有効化 + マイグレーション適用
#    (パスワードが出力される。再実行可: 既存ユーザーのパスワードは明示指定時のみ更新)
.venv/bin/python ops/setup-dev-schema.py --dev alice

# 2) tfvars 用意(出力されたパスワードと共有値を記入)
cp infra/terraform/environments/app/alice.tfvars.example \
   infra/terraform/environments/app/alice.tfvars
$EDITOR infra/terraform/environments/app/alice.tfvars
```

## デプロイ / 更新 / 破棄

```bash
ops/dev-env-up.sh alice      # build/push → plan(確認)→ apply → SPA配信 → URL表示
ops/dev-env-stop.sh alice            # CI停止(課金停止・短時間アイドル用)
ops/dev-env-stop.sh alice --start    # CI再開
ops/dev-env-down.sh alice    # アプリ層を破棄(共有基盤・ADBスキーマは保持)
```

> `terraform apply` は CLAUDE.md の承認ゲート。`dev-env-up.sh` は plan を提示し確認を取る。

## マイグレーションを後から流す

**`ops/dev-env-up.sh`（＝`make deploy`）は migration を流さない。** 配備を繰り返しても、
後から追加された migration は自分のスキーマに永久に適用されない。

| 経路 | migration | 根拠 |
|---|---|---|
| ORM ワンクリック配備（一般利用者） | **流れる** | `infra/orm/locals.tf` が `RUN_DB_BOOTSTRAP=true` を渡し、`entrypoint.sh` が `jetuse_core.bootstrap` を起動する |
| `ops/setup-dev-schema.py`（開発者の**初回**） | **流れる** | スクリプト末尾で `python -m jetuse_core.migrate` を実行する |
| `ops/dev-env-up.sh` / `ops/deploy-dev-app.sh` | **流れない** | `environments/app` は `RUN_DB_BOOTSTRAP` を設定しない |

新しい migration が入ったら、自分のスキーマへ手で流す。`.env` の `ADB_USER` / `ADB_PASSWORD` を
自分のスキーマに向けたうえで:

```bash
cd packages/api && ../../.venv/bin/python -m jetuse_core.migrate
```

`ops/setup-dev-schema.py --dev alice --app-password '<現行パスワード>'` でも流せるが、
GRANT とリソースプリンシパル設定をやり直すぶん重い。既存スキーマに対して `--app-password`
無しで実行すると「パスワードが分からないので migrate できない」と**何も変更せずに中止**する。

### 流し忘れたかどうかは `/api/health` で分かる

配備したイメージが要求する migration が DB に無ければ、`GET /api/health` の `schema` が
`behind` を返し、全体の `ok` も `false` になる。

```json
{ "ok": false,
  "schema": { "status": "behind", "applied": 21, "expected": 32,
              "pending": ["017_demos_v2", "018_demos_idx_owner", "..."],
              "hint": "このイメージが要求する migration が 11 件未適用..." } }
```

**判断材料はイメージの中にある。** DB と手元の checkout を見比べても分からない —— どちらにも
「この環境が何を要求しているか」は書かれていない。書いてあるのは動いているイメージが持つ
migration の一覧で、それが DB に無ければ答えになる。

| `schema.status` | 意味 | `ok` |
|---|---|---|
| `ok` | DB がイメージに追いついている | 落とさない |
| `behind` | **イメージが要求する版が DB に無い**。DB 系は 503 になる | **false にする** |
| `foreign` | DB のほうが先行（別系統のイメージで適用された DB を指している） | 落とさない |
| `unknown` | DB を読めない（停止・未設定など） | 落とさない（他の項目が報告済み） |

### checkout に依存する — Internal 機能を使うなら internal 系から流す

ランナーは**実行している checkout の `migrations/` だけ**を見る。Internal 固有 migration は
`internal-dev` にしか無いため、`main` / `public-dev` の checkout から流すと**適用 0 件で
正常終了する**（2026-08-04 の実害・ER-0015）。

```
main の checkout から実行          → 適用 0 件（内部固有の 017 以降がそもそも存在しない）
internal-dev の checkout から実行  → 適用 11 件
```

この取り違えのうち**片方は**ランナーが検出する。DB に適用済みなのに手元の checkout に無い版が
あれば（＝ Internal で育てた DB を Public 系の checkout から流そうとした）、適用の前に警告を出す。

```
この DB には、いまの checkout に無い migration が 11 件適用されている(017_demos_v2, ... ほか)。
いまの checkout はこの DB の系統として正しくない可能性が高い ...
```

**逆向き（DB がまだ Public 集合で、Internal 版を配備した）はランナーからは見えない。**
DB も checkout も Public 集合で差が 0 になるからで、これが 2026-08-04 に踏んだ形。
こちらは上の `/api/health` の `schema` で見る。

**症状は「アプリの不具合」に見える。** `GET /api/demos` が 503 `database unavailable` を返す。
同じ 503 は共有 ADB の停止でも起きる（ER-0018）ので、切り分けの順は
`/api/health` の `schema` → ADB の `lifecycle-state` → スキーマの有無。

## E2E検証

`URL=https://<出力されたホスト>` として:
1. `curl -o/dev/null -w'%{http_code}' $URL/` → 200(SPA)
2. `curl $URL/api/chat/models` → モデル一覧JSON(`/api/*`→本人CI 経由)
3. `curl $URL/api/db/datasets` → 200+空配列(CI→ADBを `JETUSE_ALICE` で接続)。
   **503 は「未整備」だけを意味しない** —— ADB 停止 / migration 未適用 / スキーマ未作成の
   どれでも 503 になる。切り分けは「マイグレーションを後から流す」を参照
4. Playwrightで `$URL` を開きチャット送信→ストリーム描画(auth-offならログイン不要)

## 注意点

- **同一コンパートメント必須**: IAM動的グループがリソース種別+コンパートメントで照合するため、
  per-dev CIは共有と同じコンパートメントに作る(動的グループ/ポリシーの増設は不要)。
- **イメージ更新=CI再作成=GW再デプロイ**。`dev-env-up.sh` は不変shaタグを `-var` で渡し差分を確実化。
- 共有ADBの14文字db_name上限は無関係(per-devはADBを作らない)。
- per-dev CIは共有の private サブネット/`app_nsg` を共有(各GWは自分の `ci_base_url` のみ参照)。少人数・信頼前提。
- SH サンプルの Select AI 2次バックエンドは共有 `JETUSE_APP` 上の `JETUSE_SQL_AI` プロファイル前提のため
  per-dev スキーマでは未提供(SQL Search バックエンドと datasets は per-dev でも動作)。
- 将来 開発者が増えたら `environments/app` を GitHub Actions のPRごとプレビュー環境へ昇格できる
  (remote state を OCI Object Storage に、GHA→OCI OIDC連携)。

## 開発ブランチ push → jetuse:dev 自動デプロイ（SP3-07）

`feat/**`・`fix/**`・`chore/**` への push で `.github/workflows/deploy-dev.yml` が共有プレビュー
（RM スタック `jetuse-dev-app`、jetuse:dev）へ自動デプロイする。`main` / `dev` / `internal-stable` は
トリガー対象外。**以降の実環境 E2E はこのデプロイ環境で行う**（施主指示 2026-07-09）。

- 手順の実体は `ops/deploy-dev-app.sh`（各 workflow step = 同スクリプトのサブコマンド。
  ローカルでも `ops/deploy-dev-app.sh all` で同一手順を実行できる）。
- 流れ: API イメージ build → OCIR push（tag=`<branch>-<short-sha>`、リポジトリは既存の
  `jetuse-dev-api` 系譜）→ RM スタック更新（tf 構成 zip + `image_url` 変数。他の変数は
  マージで温存）→ RM job で plan+apply → SPA build → SPA バケット同期 → gateway smoke
  （/api/health・SPA・/api/chat/models・ビルダーのモデル一覧 8 個）。
- SPA オブジェクトは **terraform 管理にしない**（SP3-07 で dev-app の spa.tf を廃止）。
  スタックはバケット+PAR のみ管理し、dist と config.json は上記 CLI アップロードが正
  （index.html を最後に公開・旧ハッシュ付きアセットは配信中クライアント保護のため残置）。
- **DB マイグレーションは自動デプロイに含めない**（`RUN_DB_BOOTSTRAP` 未設定 — 共有 loop ADB の
  スキーマ変更は人間ゲート）。マイグレーションを含むブランチは、デプロイ前に承認の上
  `ops/start-adb-if-stopped.sh && .venv/bin/python -m jetuse_core.migrate` を明示実行する
  （従来どおり。api area の deploy_cmd と同一）。
- **共有スタック 1 本のため apply は直列**: workflow の `concurrency: dev-app-deploy`
  （cancel-in-progress）+ スクリプト側の先行 RM job 完了待ちの二段構え。`destroy` は扱わない（禁止）。
- 生成（ビルダーの generate 実行）はデプロイ環境ではまだ動かない — 実 CI ランタイムは SP3-08。

### 必要な GitHub Secrets（登録は人間ゲート — 値は絶対にコミットしない）

リポジトリ Settings → Secrets and variables → Actions → New repository secret
（または `gh secret set <NAME>`）。デプロイ用 IAM ユーザーは jetuse-dev グループ相当の権限
（OCIR push / Resource Manager job / Object Storage put / stack read+update）が必要。

| Secret | 用途 | 値の入手方法 |
|---|---|---|
| `OCI_CLI_USER` | OCI API 認証（ユーザー OCID） | `~/.oci/config` の `user` |
| `OCI_CLI_TENANCY` | 同（テナンシ OCID） | `~/.oci/config` の `tenancy` |
| `OCI_CLI_FINGERPRINT` | 同（API 鍵フィンガープリント） | `~/.oci/config` の `fingerprint` |
| `OCI_CLI_KEY_CONTENT` | 同（API 秘密鍵 PEM 本文） | `cat` した `~/.oci/config` の `key_file` の中身（複数行のまま） |
| `OCI_CLI_REGION` | リージョン | `ap-osaka-1` |
| `COMPARTMENT_OCID` | jetuse:dev コンパートメント | `.env` の `COMPARTMENT_OCID` |
| `OCIR_USERNAME` | OCIR docker login | `<namespace>/<ユーザー名>`（`podman login --get-login kix.ocir.io` の値） |
| `OCIR_AUTH_TOKEN` | OCIR docker login | OCI コンソール → プロファイル → Auth Tokens で生成 |

### 環境依存値・秘匿値のシード（ORASEJAPAN 材料 / RAG_BUCKET / APP_SESSION_SECRET）

生成 gpt-5 系（共有テナンシ）の鍵材料・アプリ秘匿値は **GitHub Secrets には置かない**。
SP3-09 以降、鍵材料は **RM スタック変数にも置かない** — jetuse:dev の OCI Vault シークレット
（`jetuse-dev-app-gen-shared`、JSON: `user`/`tenancy`/`fingerprint`/`region`/`key_pem`。
vault.tf が software 保護キーの Vault ごと管理・destroy 禁止）に置く:

```bash
# ローカル（~/.oci/config に ORASEJAPAN プロファイル、.env に GEN_SHARED_PROFILE /
# GEN_SHARED_COMPARTMENT_OCID / RAG_BUCKET / APP_SESSION_SECRET がある環境）で。
# 初回は先に apply（Vault/Secret の作成）→ seed-env の順
ops/deploy-dev-app.sh seed-env
```

seed-env は ①`~/.oci` のプロファイルから鍵材料を読んで Vault シークレットへ新版投入
（実値は argv にも RM 変数にも出さない）、②非鍵材料（`gen_shared_compartment_ocid`・
`rag_bucket`・`app_session_secret`）を RM 変数へマージ、③旧 SP3-07 方式の鍵材料変数
（`gen_shared_{profile,user_ocid,tenancy_ocid,fingerprint,region,key_pem_b64}`）を
スタックから除去する（冪等 — 再実行可）。

`RAG_BUCKET` は生成 SPA バンドル（`demo-bundles/` prefix）と RAG 文書の保管バケット、
`APP_SESSION_SECRET` は生成 SPA 配信の app-session HMAC 鍵。未設定だと ready デモの
`/app/` 配信が 404 / app-session が 500 になる（fail-closed）。

コンテナ側は `GEN_SHARED_SECRET_OCID`（tf が配線・非鍵材料）を受け、共有モデルの初回
リクエスト時に `jetuse_core.gen_shared_vault` がリソースプリンシパル（DG jetuse-internal-dg）
で取得して **in-memory 署名**を構成する（鍵をディスク・環境変数に出さない）。未 seed
（placeholder）・取得失敗は共有モデルのみ 403 の fail-closed で、API は起動継続・値はログ非出力。
ローカル開発は従来どおり `GEN_SHARED_PROFILE`（~/.oci）— `GEN_SHARED_SECRET_OCID` が
設定されていればそちらが優先（fallback はしない）。

**ローテーション手順**: ~/.oci の ORASEJAPAN プロファイルを新しい API 鍵に更新 →
`ops/deploy-dev-app.sh seed-env`（シークレット新版投入）→ **API コンテナ再起動**
（`oci container-instances container-instance restart --wait-for-state SUCCEEDED`）で反映。
再起動は必須である — user principal（API 鍵）は期限切れしないため、`gen_shared_vault` は
成功した署名をプロセス生涯キャッシュし、**周期・401 の自動再取得は持たない**（いずれも同期
Vault I/O をイベントループ上で走らせるため封じた — SP3-09 codex review-2 M001）。旧鍵を
失効させる場合は、新版 seed → 再起動で新鍵が有効になったことを確認してから失効させること。

### sign_proxy の配備方式（SP3-07 で確定）

署名プロキシは **API プロセス内 mount**（`service.main` が `/gen-proxy` に mount）。
API Gateway は `/api/*` と SPA しかルートしないため公開されず、VCN 内（SP3-08 の生成 CI）
からは `http://<API CI の private IP>:8000/gen-proxy/v1` で到達する。自テナンシモデルの署名は
`AUTH_MODE=resource_principal` でリソースプリンシパル、共有テナンシは上記プロファイルを使う。

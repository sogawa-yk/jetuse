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

## 前提(一度だけ・全体)

1. 共有 `environments/dev` を `terraform apply`(本対応で**出力を追加**したため、リソース変更0で
   stateに新出力を反映する必要がある)。
2. OCIRログイン済み、`.env` に `OS_NAMESPACE`/`COMPARTMENT_OCID` 等。

## 開発者の追加(1人につき一度)

```bash
# 1) 専用スキーマ作成 + 権限 + 認証情報 + マイグレーション適用(パスワードが出力される)
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

## E2E検証

`URL=https://<出力されたホスト>` として:
1. `curl -o/dev/null -w'%{http_code}' $URL/` → 200(SPA)
2. `curl $URL/api/chat/models` → モデル一覧JSON(`/api/*`→本人CI 経由)
3. `curl $URL/api/db/datasets` → 200+空配列(CI→ADBを `JETUSE_ALICE` で接続・マイグレ適用済み。503ならDB/スキーマ未整備)
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

生成 gpt-5 系（共有テナンシ）の材料・アプリ秘匿値は **GitHub Secrets には置かない**。
RM スタックの変数（`gen_shared_*`・`rag_bucket`・`app_session_secret`）として一度だけ
シードすれば、以降のデプロイは変数マージで温存される:

```bash
# ローカル（~/.oci/config に ORASEJAPAN プロファイル、.env に GEN_SHARED_* / RAG_BUCKET /
# APP_SESSION_SECRET がある環境）で一度だけ
ops/deploy-dev-app.sh seed-env
```

`RAG_BUCKET` は生成 SPA バンドル（`demo-bundles/` prefix）と RAG 文書の保管バケット、
`APP_SESSION_SECRET` は生成 SPA 配信の app-session HMAC 鍵。未設定だと ready デモの
`/app/` 配信が 404 / app-session が 500 になる（fail-closed）。

コンテナ側は entrypoint.sh が `GEN_SHARED_*` env から `~/.oci/config` のプロファイルを冪等生成
する（未設定なら何もしない = 共有モデルは sign_proxy が 403 の fail-closed）。

### sign_proxy の配備方式（SP3-07 で確定）

署名プロキシは **API プロセス内 mount**（`service.main` が `/gen-proxy` に mount）。
API Gateway は `/api/*` と SPA しかルートしないため公開されず、VCN 内（SP3-08 の生成 CI）
からは `http://<API CI の private IP>:8000/gen-proxy/v1` で到達する。自テナンシモデルの署名は
`AUTH_MODE=resource_principal` でリソースプリンシパル、共有テナンシは上記プロファイルを使う。

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

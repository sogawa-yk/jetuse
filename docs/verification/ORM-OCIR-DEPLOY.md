# 検証レポート: ORMデプロイ失敗の原因調査とOCIR化対応

- 日付: 2026-06-24
- 対象: ORMワンクリックスタック(`jetuse-dev` コンパートメント) / `infra/orm` / `.github/workflows/release.yml`
- 関連: ADR-0011

## 1. 事象

ORMスタック `main.zip-20260624173408`(`jetuse-dev`)の APPLY ジョブが FAILED。

```
$ oci resource-manager job list --stack-id <stack> ...
APPLY  FAILED
```

## 2. RMジョブログから特定した根本原因(2件)

ジョブログ(4163エントリ)の末尾にエラー2件。**別リソース・別原因**。

### 原因① Container Instance (API) — GHCRイメージが private
```
with module.container_instance.oci_container_instances_container_instance.this
  on ../terraform/modules/container-instance/main.tf line 5
A container's image could not be pulled because the image does not exist or requires authorization.
```
- `image_url` 既定 = `ghcr.io/sogawa-yk/jetuse-api:latest`(private)。pull 認証情報も無し。
- Container Instance は外部レジストリから pull 可能だが、private には `image_pull_secrets` が要る。

### 原因② OCI Functions (fn-router) — GHCRは原理的に使用不可
```
with module.functions.oci_functions_function.router[0]
  on ../terraform/modules/functions/main.tf line 10
400-InvalidParameter, Invalid image - The image must be an OCIR image in this region's registry
```
- `router_image` 既定 = `ghcr.io/sogawa-yk/jetuse-fn-router:latest`。
- **OCI Functions は同一リージョンの OCIR イメージのみ受付**。GHCR public 化でも回避不可。

### 設計ギャップ
`module.ocir` は OCIR リポジトリを作る(既定 `api` のみ)のに、`release.yml` は GHCR にしか
push していなかった。OCIR は空のまま。

## 3. 対応(ADR-0011: 選択肢A = OCIRに寄せる)

| ファイル | 変更 |
|---|---|
| `docs/decisions/ADR-0011-*.md` | 決定記録(GHCR→OCIR) |
| `infra/orm/variables.tf` | `ocir_namespace`/`ocir_region_key` 追加。`api_image_url`/`fn_router_image` 既定を `""`(=合成)へ |
| `infra/orm/locals.tf` | OCIRパスを合成(`<region_key>.ocir.io/<namespace>/<prefix>-{api,fn-router}:latest`) |
| `infra/orm/main.tf` | `module.ocir` に `repositories=["api","fn-router"]`。modules へ `local.*_image` を渡す |
| `infra/orm/schema.yaml` | OCIR変数をUIに追加、画像URLは上書き任意項目に |
| `.github/workflows/release.yml` | GHCR に加え OCIR(`kix.ocir.io`)へも push。OCIRログイン追加 |

確定値: OCIRネームスペース=`idqcucnenh88` / リージョンキー=`kix` / 既存repo=`jetuse-api`(private, 作成済)。
`jetuse-fn-router` は本変更の apply で作成される。

## 4. 静的検証(実施済み)

```
$ terraform fmt -check -recursive infra        # exit 0
$ cd infra/orm && terraform init -backend=false && terraform validate
Success! The configuration is valid.
$ python3 -c "yaml.safe_load(...)"  release.yml / schema.yaml   # OK
```

## 5. 残作業(実デプロイ通過に必要 — 未完)

1. **GitHub Secrets 設定**(`release.yml` の OCIR push 用):
   - `OCIR_USERNAME` = `idqcucnenh88/<user>`(Identity Domain時は `idqcucnenh88/oracleidentitycloudservice/<user>`)
   - `OCIR_TOKEN` = OCI Auth Token
2. **イメージを OCIR へ publish**: 本PRが main にマージされ `release.yml` が成功 → OCIR に
   `jetuse-api` / `jetuse-fn-router` の `latest` が入る。
   (リポジトリは push 前に存在が必要。`jetuse-fn-router` は先にスタックの ocir 部分を apply して作成。)
3. **ORM 再 APPLY**: 新スタック zip(本変更込み)で再デプロイ → 原因①②の解消を確認。
4. 結果を本レポートに追記して完了とする(実機検証主義)。

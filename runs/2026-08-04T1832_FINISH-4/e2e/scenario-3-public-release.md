# E2E-3: Public リリース `public-v0.1.0`

対象: `public-dev → main`（PR #138 / 21コミット）と、それが生む配布物
実 OCI・実 GitHub に対して実施。モック不使用。

## このリリースが配布物に与える変更

`main..public-dev` の差分のうち、**利用者に届くのは1点だけ**。

```
infra/terraform/modules/hosted-agent/scripts/{delete_agent,ensure_agent,lib,mock_oci_for_tests,smoke_test}.sh
```

PR #128「削除処理が管理外のリソースを巻き込む不具合を直す」による。
**変わらないもの**: `packages/api` / `packages/web/src` / `packages/web/dist` / `infra/orm`。

この事実が E2E の範囲を決めている。アプリの挙動は変わらないので、確かめるべきは
「配布物が健全で、意図した修正が入っていること」。

## 結果

| # | 確認 | 実測 | 判定 |
|---|---|---|---|
| 3a | release workflow が成功 | `images: success` / `spa: success` | PASS |
| 3b | **ブランチ HEAD が更新されない** | マージ直後 `aad35f70` → workflow 後 `aad35f70` | PASS |
| 3c | `orm-main` の ZIP が更新される | `updated=2026-08-04T10:09:52Z` / size 4,303,542 | PASS |
| 3d | 公開 ZIP を認証なしで取得できる | `curl` で取得成功（利用者と同じ経路） | PASS |
| 3e | ZIP に必須ファイルが揃う | `schema.yaml` / `main.tf` / `packages/web/dist/index.html` | PASS |
| 3f | **今回の修正が配布物に入っている** | hosted-agent scripts 5本すべてが `main` と一致 | PASS |
| 3g | ZIP が `terraform validate` を通る | `Success! The configuration is valid.` | PASS |
| 3h | ZIP が実 OCI に対して `terraform plan` を通る | `Plan: 185 to add, 0 to change, 0 to destroy.`（`jetuse:test`） | PASS |
| 3i | 公開イメージを匿名で pull できる | 5リポジトリすべて manifest 取得可 | PASS |
| 3j | 削除ガード（唯一の挙動変更）が動く | `smoke_test.sh` 2 passed | PASS |

### 3b が重要な理由

ADR-0030 で release workflow の dist 直接 commit を削除した効果の確認。
以前は毎リリースで `chore(spa): rebuild dist for ORM deploy` が `main` に積まれていた
（履歴に3件残っている）。branch protection と両立しないため廃止したので、
**HEAD が動かないこと**が正しい挙動になる。

### 3i の手順

OCIR は public リポジトリでも Bearer トークンを要求する。空の認証設定で確認した。

```
DOCKER_CONFIG=<空> docker manifest inspect kix.ocir.io/<ns>/jetuse-api:latest
```

`jetuse-api` / `jetuse-fn-router` / `jetuse-agent-{openai,langgraph,adk}` の5本すべてで取得可。
OCI CLI 側でも `is-public: True` を確認済み。

## 実施しなかったこと

**フルの ORM apply は行っていない。**

- 今回のリリースに**アプリコードの変更が無い**（`infra/orm` も `dist` も不変）ため、
  `ops/e2e/public-deploy.mjs`（38項目のブラウザ E2E）が検証する対象が変わっていない
- CLAUDE.md は本番相当の Terraform apply を**人間ゲート**としている
- 代わりに、実 OCI に対する `terraform plan`（185リソース）まで到達させた。
  スタック自身の入力検証（prefix の長さ制約）が働くことも確認している

配布物に**アプリコードの変更が入るリリース**では、`ops/e2e/public-deploy.mjs` による
実デプロイ E2E を実施すること。

## 途中で潰した誤検出

- `schema.yaml` に `home_region` が無いのを欠落と疑ったが、`infra/orm/locals.tf:31` で
  `data.oci_identity_region_subscriptions` から**自動検出**する実装に変わっており、
  入力項目が無いのが正。ADR-0014 Decision 8（明示入力）は陳腐化している
- ZIP の `delete_agent.sh` が repo と不一致に見えたのは、ローカル `main` が
  fetch 済み・未 fast-forward だっただけ。同期後は一致

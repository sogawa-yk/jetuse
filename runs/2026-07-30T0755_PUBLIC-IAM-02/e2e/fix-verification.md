# スタック修正（誤誘導メッセージの解消）と実機確認

## 直した問題

デプロイ担当グループに `inspect tenancies in tenancy` が無いと、
`data.oci_identity_region_subscriptions` は **401/404 ではなく `null`** を返す。
その結果、修正前は plan が次の3件で落ち、**どれも本当の原因（権限）を指していなかった**。

```text
Error: Resource precondition failed   （「このリージョンは未対応です」）
Error: Resource precondition failed   （「GenAI が未検証のリージョンです」）
Error: Iteration over null value      （providers.tf の home リージョン解決）
```

## 修正内容

| ファイル | 変更 |
|---|---|
| `infra/orm/locals.tf` | `region_subscriptions_readable = try(length(...) > 0, false)` を追加。既存の `try(..., "")` が null を "" に丸めて原因を消していた |
| `infra/orm/main.tf` | `region_guard` の**先頭**に、不足している文を名指しする precondition を追加。後続のリージョン判定2件は `!local.region_subscriptions_readable \|\|` で**購読一覧が読めているときだけ**評価する |
| `infra/orm/providers.tf` | `home` provider の生の for 式（`Iteration over null value` の発生源）を `local.home_region`（`try` 付き）参照へ |

## 実機確認

作業ツリーから作った zip を ORM スタックへ差し替え、`inspect tenancies` を持たない
デプロイ担当（コンパートメント3文のみ）で plan を実行した。

**修正後の出力（1件だけ・原因を名指し）:**

```text
Error: Resource precondition failed
  on main.tf line 13, in resource "terraform_data" "region_guard":
  13:       condition     = local.region_subscriptions_readable
    ├────────────────
    │ local.region_subscriptions_readable is false

テナンシのリージョン購読一覧を取得できませんでした。デプロイ実行ユーザーのグループに
`Allow group <deployer-group> to inspect tenancies in tenancy` が必要です(この1文が無いと
Terraform は権限エラーではなく null を受け取り、リージョン判定とホームリージョン解決が
同時に壊れます)。詳細は docs/setup/public-deploy-dedicated-compartment.md。
```

`Iteration over null value` は消え、誤誘導していたリージョン系の2件も出なくなった。
plan ジョブのログ中に `Error: Resource precondition failed` は **1件だけ**、
`inspect tenancies in tenancy` の文字列も **1回だけ**現れる（機械的に数えて確認）。

### 実施の経緯（2回やり直している）

1回目は**修正が zip に入っていなかった**。`scripts/package-orm-stacks.sh` は `git archive HEAD` を
使うため、未コミットの変更は反映されない（`pitfalls.md` §3）。
2回目は手動パッケージしたが `.terraform`（262MB のプロバイダ）を含めてしまい 81MB になり、
`stack update --config-source` が通らず**古い zip のまま** plan していた（同 §2・§4）。
3回目に `.terraform` を除いた 4.1MB の zip で差し替え、`lifecycle-state: ACTIVE` を確認してから
plan して、上記の結果を得た。

権限が揃っている状態（`inspect tenancies` あり）では従来どおり plan が成功することも、
同じスタックで確認している（Phase B の plan / 必要性検証の TENANCIES ケース）。

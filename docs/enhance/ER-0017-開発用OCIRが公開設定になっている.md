---
id: ER-0017
title: 開発用 OCIR が公開設定になっている
status: parked
size: S
source: 気づき
created: 2026-08-05
ticket:
pr:
---

## ひとことで

社内開発用のコンテナイメージ置き場が、誰でも取得できる設定になっている。

## 何が起きているか

`jetuse:dev` の OCIR リポジトリのうち **2本が公開設定**。

| リポジトリ | 公開 | 用途（推定） |
|---|---|---|
| `jetuse-dev-api` | 非公開 | 開発中の API イメージ |
| `jetuse-dev-web` | 非公開 | 開発中の SPA |
| **`jetuse-dev-build`** | **公開** | 用途未確認 |
| **`jetuse-dev-gen`** | **公開** | 生成ランタイム用（SP3-08）と推定 |

**公開である理由が確認できていない。** 同一テナンシ内の Container Instance が pull するだけなら、
認証つき（非公開）で足りるはず。

配布用の `jetuse:registry` 側が公開なのは**必要**（外部利用者が Deploy ボタンから引くため）。
こちらは開発用なので事情が違う。

## 根拠

2026-08-05 の実測:

```
$ oci artifacts container repository list --compartment-id <jetuse:dev> \
    --query 'data.items[?"is-public"==`true`].{name:"display-name"}'
jetuse-dev-build
jetuse-dev-gen
```

## どう直すか

1. **まず理由を確認する。** 公開でないと動かない経路があるか調べる
   （`infra/images/gen-ci/` と `ops/deploy-dev-app.sh gen-images` の周辺）
2. 理由が無ければ非公開に変える
3. 理由がある（例: 特定のサービスが匿名 pull を要求する）なら、**その理由を
   `docs/setup/` に書き残す**。書いておかないと次に見た人が同じ疑問を持つ

## やらない場合の代償

開発中のイメージが外部から取得できる状態が続く。中身は JetUse のコードで、
**リポジトリ自体が public なので新たな情報漏洩ではない**が、意図しない公開設定が
放置されているのは望ましくない。

## 関連

- ADR-0028（`jetuse:registry` = 配布物の置き場・公開が必要）
- `runs/2026-08-04T1832_FINISH-4/`（コンパートメント棚卸しの記録）

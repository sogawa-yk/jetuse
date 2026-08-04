# 検証資源の後片付け

この run が作ったものは全部消した（`jetuse-spike-prep04-` 接頭辞 / run 固有スキーマ）。

```
spikes/prep04/teardown.py --yes
  file jetuse-spike-prep04-…-通常.xlsx        -> True
  file jetuse-spike-prep04-…-リクエスト例.xlsx -> True
  Vector Store 削除: jetuse-spike-prep04-98af71（名前照合済み）

spikes/ragm02/teardown.py --yes
  台帳と一致（USER_ID / 作成時刻 / マーカーの 3 点）
  DROP USER JETUSE_PREP04_98AF71 CASCADE
  削除後の再照会: ユーザー 0 件 / ACL 0 件
  ローカルの認証資材を削除: secrets.json, ledger.json, schema.txt, wallet/
```

ADB 自体は増やしていない（共有 loop ADB のスキーマだけで隔離した）。

## この run で追加で消したもの（報告対象）

E2E の開始時、Vector Store の作成が
`400 LimitExceeded: A tenancy is allowed to have a maximum of 10 completed vector stores` で
失敗した。**PREP-03 の検証用の箱 `jetuse-spike-prep03-ee2fb4` が残っていた**ため、これを削除して
枠を空けた（`jetuse-spike-` 接頭辞の検証用リソースなので CLAUDE.md の許可範囲）。PREP-03 は
2026-08-01 にマージ済みで、その run の後片付けから漏れていたものと思われる。

> テナンシの完了済み Vector Store は 10 個上限。デモ用の箱（`jetuse-rag-demo_…`）が
> 増えると、検証タスクが箱を作れなくなる。**人間側で棚卸しの要否を判断されたい。**

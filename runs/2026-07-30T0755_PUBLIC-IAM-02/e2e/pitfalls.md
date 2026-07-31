# この検証で踏んだ落とし穴（測定の信頼性に関わるもの）

## 1. `oci iam policy update` は `--statements` 単独では**何も更新しない**

```text
If updating either statements or version date, both parameters must be specified.
```

`--version-date ""` を併記して初めて更新される。スクリプトがこの標準エラーを
`>/dev/null 2>&1` で捨てていたため、**ポリシーが変わっていないのに「文を外しても plan が通る」**
という誤った結論を4ケースぶん出した。

対策として、変更操作のあとに必ず状態を読み直してアサートする:

```bash
oci iam policy get --policy-id "$P" --query 'length(data.statements)' --raw-output
```

## 2. ORM スタックの config 差し替えも同じく黙って失敗しうる

`resource-manager stack update --config-source <zip>` を `>/dev/null 2>&1` で実行していたため、
**古い zip のまま plan していた**のに気づかなかった（修正したはずのコードのエラーが再現し続けた）。
更新後は `job get-job-logs-content` に**新しいコードの痕跡**があるかを確認する。

## 3. 配布 zip は「コミット済みの内容」から作られる

`scripts/package-orm-stacks.sh` は `git archive HEAD` を使う（ignore 対象の混入を防ぐ設計）。
そのため**未コミットの修正は zip に入らない**。コミット前に実機で確かめたい場合は、
作業ツリーから手動でステージして zip を作る必要がある。

## 4. 手動パッケージでは `.terraform` を必ず除外する

`infra/terraform/modules/iam/.terraform/` にプロバイダバイナリ（262MB）が残っており、
素直にコピーすると zip が **81MB** になって ORM へのアップロードが通らない。
（公式スクリプトが `.terraform` の混入チェックを持っているのはこのため。）
除外後は 4.1MB で、リリース zip（4.3MB）と同等になった。

## 5. IAM の書き込みはホームリージョンへ

プロファイルの既定リージョン（`us-chicago-1`）のままだと `iam compartment create` 等が失敗する。
`--region ca-toronto-1`（ホーム）を明示する。

## 6. 新規ユーザーの API キーはアップロード直後 401

実測で 2〜3 分後に成功。権限設定の誤りと区別がつかないので、待ってから切り分ける。

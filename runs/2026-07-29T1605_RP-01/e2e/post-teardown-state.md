# 片付け後の共有 ADB の状態（read-only）

検証開始前と同じ状態に戻っていること、および JETUSE_APP に OCI$RESOURCE_PRINCIPAL のEXECUTE が既に付いていることを read-only で確認する。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/…（post-teardown 照会）
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
users: ['JETUSE_APP', 'JETUSE_APP_Q', 'JETUSE_SP1_02', 'JETUSE_SP1_03', 'JETUSE_SP2_01', 'JETUSE_SP2_02', 'JETUSE_SP2_02_Q']
creds: [('ADMIN', 'OCI$RESOURCE_PRINCIPAL'), ('JETUSE_APP', 'JETUSE_OCI_CRED'), ('JETUSE_SP2_02', 'JETUSE_OCI_CRED')]
RP EXECUTE を持つ JETUSE 系スキーマ: ['JETUSE_APP']
buckets: ['jetuse-dev-app-spa', 'jetuse-dev-app-speech', 'jetuse-spike-sp202-rag', 'jetuse-spike-sp303-e2e']
  ウォレットとパスワードファイルを削除
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `post-teardown-state.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

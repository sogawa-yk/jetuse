# シナリオ4（対照）: 誤ったプロファイル由来の API キー資格情報

削除した旧パーサを再現して作った資格情報では同じ URL が ORA-20404 になり、OCI$RESOURCE_PRINCIPAL では 200 になることを、同一 DB・同一スキーマ・同一 URL で対照する。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/driver.py 4
== シナリオ4（対照）: 誤ったプロファイル由来の API キー資格情報 ==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  ~/.oci/config のプロファイル: ['DEFAULT', 'ORASEJAPAN', 'DEPLOYTEST']
  旧パーサが拾う値: 最後のプロファイル [DEPLOYTEST] のもの
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  資格情報作成: JETUSE_SPIKE_RP01D389_BADCRED（CREATE_CREDENTIAL 自体は成功する＝ここでは気づけない）
  同じ URL を JETUSE_SPIKE_RP01D389_BADCRED で叩く: ORA-20404: Object not found - https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/?compartmentId=ocid1.compartment.oc1..<REDACTED>
  同じ URL を OCI$RESOURCE_PRINCIPAL で叩く: HTTP 200
  片付け: JETUSE_SPIKE_RP01D389_BADCRED を削除（残存 0 件。API キーを ADB に残さない）
判定: PASS（旧方式は ORA-20404 / 新方式は 200。本変更が原因を潰したことの対照）
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `scenario-4.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

# シナリオ2: OCI$RESOURCE_PRINCIPAL で Object Storage 200

API キー資格情報を一切作らずに DBMS_CLOUD.SEND_REQUEST で Object Storage を叩き 200 を得る。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/driver.py 2
== シナリオ2: OCI$RESOURCE_PRINCIPAL で Object Storage 200 ==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  この接続から見える資格情報: [('ADMIN', 'OCI$RESOURCE_PRINCIPAL', 'TRUE')]
  ListBuckets(dev コンパートメント): HTTP 200
  GetBucket(共有 SPA バケット): HTTP 200
判定: PASS（API キー資格情報を作らずに Object Storage を読めている）
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `scenario-2.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

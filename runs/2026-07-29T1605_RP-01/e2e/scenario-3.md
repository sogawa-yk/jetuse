# シナリオ3: OCI$RESOURCE_PRINCIPAL でベクトル索引の作成と検索

rag_select_ai.py と同じ呼び出し（CREATE_PROFILE / CREATE_VECTOR_INDEX / GENERATE）をリソースプリンシパルで実行し、文書固有の合言葉が検索で返ることを確認する。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/driver.py 3
== シナリオ3: OCI$RESOURCE_PRINCIPAL でベクトル索引の作成と検索 ==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  バケット作成: jetuse-spike-rp01d389-rag（etag を作成応答から記録）
  文書投入: 2 件
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  CREATE_PROFILE: JETUSE_SPIKE_RP01D389_PROF（credential_name=OCI$RESOURCE_PRINCIPAL）
  CREATE_VECTOR_INDEX: JETUSE_SPIKE_RP01D389_IDX（object_storage_credential_name=OCI$RESOURCE_PRINCIPAL）
  索引の取り込み行数: 2
  取り込み対象: ['doc-a.txt', 'doc-b.txt']

  Q: ドキュメントAの合言葉は何か。
  A: ドキュメントAの合言葉はBLUEHERONです。

Sources:
  - doc-a.txt (https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/jetuse-spike-rp01d389-rag/o/rag/rp01d389/doc-a.txt)
  → 期待語 BLUEHERON を含む: True

  Q: ドキュメントBの合言葉は何か。
  A: ドキュメントBの合言葉はREDFALCONである。

Sources:
  - doc-b.txt (https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/jetuse-spike-rp01d389-rag/o/rag/rp01d389/doc-b.txt)
  → 期待語 REDFALCON を含む: True

判定: PASS（索引作成・取り込み・検索のすべてがリソースプリンシパルで成立）
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `scenario-3.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

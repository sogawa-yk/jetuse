# アップロード（実 API・アプリ経路）

`POST /api/rag/files`（multipart + `attributes`）に架空の xlsx を v1.0 → v2.0 の順で投げた。

```
jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx v1.0 | file_id=f0cea26d-9b1c-498a-8d1c-1dee7a3f777a | oci_file=file-kix-bed… | status=completed | backends={'vector_store': 'indexed', 'select_ai': 'error', 'opensearch': 'disabled', 'adb': 'indexed'}
jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx v2.0 | file_id=ee486231-584d-4d36-8b7b-f7e77fce6c2b | oci_file=file-kix-045… | status=completed | backends={'vector_store': 'indexed', 'select_ai': 'error', 'opensearch': 'disabled', 'adb': 'indexed'}
```

- `backends.vector_store` = マネージド側の処理状態 / `backends.adb` = 自前索引の取り込み状態
- スキーマ: `JETUSE_PREP01_E48127`（共有 loop ADB 内の run 固有スキーマ。ADB は増やしていない）

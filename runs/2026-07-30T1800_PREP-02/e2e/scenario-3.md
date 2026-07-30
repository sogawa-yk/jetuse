# シナリオ3: アプリの実経路で、表示が観測と一致することを示す

シナリオ1・2 は「OCI 側パイプラインが xlsx をどうするか」の観測だった。ここでは
**本番と同じ関数**（`rag.add_file` → `rag_select_ai.ensure_profile` →
`rag.attach_backend_status`）を、この run のスキーマ・バケットに向けて実行し、
取り込み状況バッジが実測と一致することを確かめる。

向き先は環境変数で run 固有の隔離先に差し替えている（共有スキーマ・共有バケットは触らない）。
GenerativeAI プロジェクトは `jetuse:dev` の既存 ACTIVE なものを使う（**作成しない**）。

```console
$ .venv/bin/python -u runs/2026-07-30T1800_PREP-02/e2e/driver.py 3
  向き先: schema=JETUSE_PREP02D770 / bucket=jetuse-spike-prep02d770-rag
  GenAI プロジェクト: jetuse-loop-project（既存を使用・作成しない）
== シナリオ3: アプリの実経路（アップロード → 索引 → バッジ）==
  add_file: prep02-fake-workbook.xlsx -> id=1d8f7221-92dc-4232-9482-61ac81742a66 status=processing
  add_file: prep02-control.md -> id=79ea0c3b-3af7-485e-847e-8b3569d61114 status=processing
  ensure_profile: profile=JETUSE_RAG_73590213 / index=JETUSE_RAGIDX_73590213
  索引に居る file_id: ['1d8f7221-…', '79ea0c3b-…']
  prep02-control.md: backends={'vector_store': 'pending', 'select_ai': 'indexed', 'opensearch': 'disabled', 'adb': 'indexed'}
  prep02-fake-workbook.xlsx: backends={'vector_store': 'pending', 'select_ai': 'indexed', 'opensearch': 'disabled', 'adb': 'indexed'}

観測: xlsx の select_ai バッジ = indexed（PREP-01 の実装なら拡張子だけを見て 'error' だった）
```

本番の索引（`JETUSE_RAGIDX_73590213$VECTAB`）の中身もシナリオ1 と同じ形だった
（xlsx 由来 1 チャンク・印字可能文字 99.0%・シート名と A1 見出しを含む・日本語も化けない）:

```
    attributes: {'object_name': '1d8f7221-…_prep02-fake-workbook.xlsx', 'object_size': 5634,
      'location_uri': 'https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/jetuse-spike-prep02d770-rag/o/rag/prep02-prep02d770/',
      'start_offset': 1, 'end_offset': 200} / 本文の全長=195

    制約
        A  B  C
     1  項目  値  備考
     2  最大同時接続数  100  合言葉 ZEBRAFINCH
     3  応答時間 SLA  300ms  95 パーセンタイル
    改訂履歴
        A  B  C
     1  版  日付  内容
     2  v2.0  2026-07-30  架空の検証用ブック（PREP-02）
```

## 判定

- 本タスクの変更後、xlsx の `select_ai` バッジは **`indexed`**。PREP-01 の実装なら
  同じ状況でも拡張子だけを見て **`error`** を出していた（`runs/2026-07-30T1600_PREP-01/e2e/upload.md`
  の実出力が `'select_ai': 'error'`）。表示が実測に追いついた。
- `vector_store` が `pending` なのはマネージド側の処理待ち（本タスクと無関係。PREP-01 で
  `completed` になることを実測済み）。`opensearch` はこの環境で無効。

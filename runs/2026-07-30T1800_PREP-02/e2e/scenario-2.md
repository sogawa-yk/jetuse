# シナリオ2（切り分け）: xlsx **だけ**の場所に索引を作る

シナリオ1は xlsx と txt の混在だったので、「txt があったから索引が成立しただけ」の
可能性を潰す。同じバケットの別プレフィックス（`rag/prep02d770-xlsxonly/`）に架空 xlsx を
1 件だけ置き、専用のプロファイルと索引を作った。

```console
$ .venv/bin/python -u runs/2026-07-30T1800_PREP-02/e2e/driver.py 2
== シナリオ2: xlsx のみの場所に索引を作る（切り分け）==
  投入: rag/prep02d770-xlsxonly/…c3_prep02-fake-workbook.xlsx（5635 bytes）
  location: <OBJECT_STORAGE>/b/jetuse-spike-prep02d770-rag/o/rag/prep02d770-xlsxonly
  CREATE_PROFILE: JETUSE_SPIKE_PREP02D770_PROFX
  CREATE_VECTOR_INDEX: JETUSE_SPIKE_PREP02D770_IDXX（成功）

  $VECTAB の行数: 1
  JETUSE_SPIKE_PREP02D770_IDXX$VECTAB の列: [('CONTENT', 'CLOB'), ('ATTRIBUTES', 'JSON'), ('EMBEDDING', 'VECTOR')]
  オブジェクト別のチャンク数:
    …c3_prep02-fake-workbook.xlsx: 1
  チャンク本文（CONTENT の先頭 2000 文字）:
    --- …c3_prep02-fake-workbook.xlsx doc_id=1 embed_id=1 / 長さ=195 / 印字可能文字 99.0% ---

    制約

        A  B  C

     1  項目  値  備考

     2  最大同時接続数  100  合言葉 ZEBRAFINCH
    （以下シナリオ1と同一）

  Q: 最大同時接続数の備考に書かれている合言葉は何か。
  A: 最大同時接続数の備考に書かれている合言葉は、ZEBRAFINCH です。

Sources:
  - …c3_prep02-fake-workbook.xlsx (…/rag/prep02d770-xlsxonly/…)
  → 期待語 ZEBRAFINCH を含む: True

観測: xlsx 単独 索引行数=1 / 合言葉ヒット=True
```

## 判定

xlsx が 1 件だけでも索引は作られ、本文はテキストとして読め、検索でも引ける。
シナリオ1 の結果は「同居している txt のおかげ」ではない。

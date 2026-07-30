# シナリオ1: Select AI の索引が xlsx をどう扱うか（xlsx + txt 混在＝本番と同じ形）

実施日 2026-07-30 / 共有 loop ADB（`jetuse-loop-adb` / 23.26.3.1.0 / ap-osaka-1 / `jetuse:dev`）。
検証用スキーマ `JETUSE_PREP02D770`・バケット `jetuse-spike-prep02d770-rag` はこの run が作り、
`teardown.md` で削除した。**ADB は増やしていない。顧客データは使わず、架空のブックだけを使った。**

架空 xlsx（`制約` / `改訂履歴` の 2 シート・`合言葉 ZEBRAFINCH` をセルに含む）と、
対照のプレーンテキスト（`合言葉は BLUEHERON`）を同じ場所に置き、索引を作った。

```console
$ .venv/bin/python -u runs/2026-07-30T1800_PREP-02/e2e/driver.py 1
== シナリオ1: Select AI の索引が xlsx をどう扱うか（混在。本番と同じ形）==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  （検証用スキーマ作成・Select AI 権限付与は setup-dev-schema.py / setup-select-ai.py。全文は scenario-1.log）
  バケット作成: jetuse-spike-prep02d770-rag（etag を作成応答から記録）
  投入: rag/prep02d770/…a1_prep02-fake-workbook.xlsx（5635 bytes）
  投入: rag/prep02d770/…b2_prep02-control.txt（221 bytes）
  location: <OBJECT_STORAGE>/b/jetuse-spike-prep02d770-rag/o/rag/prep02d770
  CREATE_PROFILE: JETUSE_SPIKE_PREP02D770_PROF
  CREATE_VECTOR_INDEX: JETUSE_SPIKE_PREP02D770_IDX（成功）

  $VECTAB の行数: 2
```

## (a) 索引作成: **成功**（エラーは出ていない）

`DBMS_CLOUD_AI.CREATE_VECTOR_INDEX` は例外を投げずに返った。xlsx が混ざっていても
索引全体が失敗することはなく、対照の txt も一緒に取り込まれた。

## (b) `$VECTAB` の中身: **読めるテキスト**（バイナリのゴミではない）

```console
  JETUSE_SPIKE_PREP02D770_IDX$VECTAB の列: [('CONTENT', 'CLOB'), ('ATTRIBUTES', 'JSON'), ('EMBEDDING', 'VECTOR')]
  オブジェクト別のチャンク数:
    …a1_prep02-fake-workbook.xlsx: 1
    …b2_prep02-control.txt: 1
```

xlsx 由来の行（`CONTENT`。**印字可能文字 99.0%** / 全長 195 文字。空白と改行の並びは実出力のまま）:

```
    attributes: {'object_name': '…a1_prep02-fake-workbook.xlsx', 'object_size': 5635,
      'last_modified': '2026-07-30T09:06:54+00:00',
      'location_uri': 'https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/jetuse-spike-prep02d770-rag/o/rag/prep02d770/',
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

読み取れること:

- 日本語が文字化けしていない。数値も文字列も入っている。
- **シート名（`制約` / `改訂履歴`）と、A1 形式の列見出し・行番号が本文に含まれる**。
  つまり DB 側（Oracle Text）の抽出は表構造をある程度保っている。
- ただしチャンクは **1 ファイル = 1 チャンク**（`start_offset=1 / end_offset=200`）。
  `attributes` に入るのは `object_name` / `object_size` / `last_modified` / `location_uri` /
  オフセットだけで、**シート名・セル範囲の構造化メタデータは付かない**（それは `adb` の役目）。

## (c) 検索: xlsx の中身が引ける

```console
  Q: 最大同時接続数の備考に書かれている合言葉は何か。
  A: 最大同時接続数の備考に書かれている合言葉は、ZEBRAFINCH です。

Sources:
  - …a1_prep02-fake-workbook.xlsx (https://objectstorage.<region>.oraclecloud.com/…/prep02-fake-workbook.xlsx)
  → 期待語 ZEBRAFINCH を含む: True

  Q: 対照ドキュメントの合言葉は何か。
  A: 対照ドキュメントの合言葉は BLUEHERON である。
  → 期待語 BLUEHERON を含む: True

観測: 索引行数=2 / 検索ヒット={'xlsx 由来': True, 'txt 由来': True}
```

## 判定

**「索引作成が失敗する」でも「本文が壊れる」でもなく、正常に扱える。**
`tasks/PREP-02.md` の 3 択のうち 3 つ目（恒久 `error` を撤回する）に該当する。

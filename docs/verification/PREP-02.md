# PREP-02 検証レポート: Select AI バックエンドは xlsx をどう扱うのか（実測）

- 日付: 2026-07-30 / リージョン: ap-osaka-1 / 対象: `select_ai` バックエンド
- 環境: 共有 loop ADB（`jetuse-loop-adb` / Oracle AI Database 26ai 23.26.3.1.0 / コンパートメント
  `jetuse:dev`）。**ADB は増やしていない**。検証用スキーマ（run 固有名 `JETUSE_PREP02D770`）と
  バケット（`jetuse-spike-prep02d770-rag`）で隔離し、検証後に削除した（`teardown.md`）。
- 証跡: `runs/2026-07-30T1800_PREP-02/e2e/`（`scenario-1..3.md` / `teardown.md` / `SKIPPED.md` /
  実行に使った `driver.py`）。OCID・ネームスペース・リージョンは
  `spikes/spike_m1/redact_evidence.py` で伏字化してある。原本の `*.log` は `.gitignore` 対象。
- 使ったデータは**架空のブックのみ**（`制約` / `改訂履歴` の 2 シート・合言葉 `ZEBRAFINCH` 入り）。

## 結論（先に）

**Select AI のベクトル索引は .xlsx を正常に取り込める。** 索引作成は成功し、`$VECTAB` の本文は
読めるテキスト（日本語も化けない）で、検索でも xlsx 内のセル値が引ける。

したがって PREP-01 が置いた「**xlsx は恒久 `error`**」（`rag.SELECT_AI_EXTENSIONS` による
拡張子だけの判定）は**誤りであり、撤回した**。あれは実機・一次資料の裏付けを持たない推測だった
（`runs/2026-07-30T1600_PREP-01/e2e/SKIPPED.md` §2 に「実機での確認は未実施」と明記されていた）。

| `tasks/PREP-02.md` の 3 択 | 実測 |
|---|---|
| 索引作成が失敗する | **該当しない**（成功した） |
| 索引はできるが本文が壊れる | **該当しない**（読めるテキストだった） |
| **正常に扱える** | **これ**。恒久 `error` を撤回した |

## 一次資料の有無

**xlsx（`.xlsx`）の可否を明記した一次資料は見つからなかった。** 記載は「例示 + 別表への委譲」で
止まっている。以下は確認した範囲（2026-07-30 時点）:

| 資料 | 記載 |
|---|---|
| [Select AI with RAG](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/select-ai-retrieval-augmented-generation.html) | 「converting input documents (for example, PDF, DOC, JSON, XML, or HTML) …. Oracle Text supports around 150 file types.」＝**例示のみ**で、完全な一覧は Oracle Text の対応形式へ委譲 |
| [DBMS_CLOUD_AI Package（`location` 属性）](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/dbms-cloud-ai-package.html) | 「The files in this location can be documents in formats such as PDF, DOC, JSON, XML, or HTML.」＝同じく例示 |
| [Oracle Text Supported Document Formats](https://docs.oracle.com/en/database/oracle/oracle-database/26/ccref/oracle-text-supported-document-formats.html) | 表計算の節に `Microsoft Excel for Windows 3.0 – 2019` 等。ただし**`.xlsx` という語での明示は無い**（バージョン範囲からの推定になる） |

つまり「Select AI が xlsx を読めるか」を一次資料だけで断定することはできない。
**本レポートの根拠は実測である。**

## 何を実測したか

### 1. 索引作成は成功する（xlsx + txt 混在 = 本番と同じ形）

架空 xlsx と対照テキストを同じ場所に置き、`DBMS_CLOUD_AI.CREATE_VECTOR_INDEX` を実行した。
例外は発生していない（エラーコード・エラー本文は**無い**）。全文は `scenario-1.md`。

```console
$ .venv/bin/python -u runs/2026-07-30T1800_PREP-02/e2e/driver.py 1
  投入: rag/prep02d770/…a1_prep02-fake-workbook.xlsx（5635 bytes）
  投入: rag/prep02d770/…b2_prep02-control.txt（221 bytes）
  CREATE_PROFILE: JETUSE_SPIKE_PREP02D770_PROF
  CREATE_VECTOR_INDEX: JETUSE_SPIKE_PREP02D770_IDX（成功）

  $VECTAB の行数: 2
  オブジェクト別のチャンク数:
    …a1_prep02-fake-workbook.xlsx: 1
    …b2_prep02-control.txt: 1
```

### 2. `$VECTAB` に入るのは読めるテキスト（バイナリのゴミではない）

xlsx 由来の行の `CONTENT`（**印字可能文字 99.0%** / 全長 195 文字）:

```
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

- 日本語が化けない。数値も文字列も入る。
- **シート名（`制約` / `改訂履歴`）と A1 形式の列見出し・行番号が本文に含まれる。**
- チャンクは **1 ファイル = 1 チャンク**（`start_offset=1 / end_offset=200`）。
  `attributes` は `object_name` / `object_size` / `last_modified` / `location_uri` /
  オフセットのみで、**シート名・セル範囲の構造化メタデータは付かない**。
  セル単位の出典が要るなら従来どおり `adb` バックエンド（ADR-0020 の能力差はそのまま）。

### 3. 検索で xlsx の中身が返る

```console
  Q: 最大同時接続数の備考に書かれている合言葉は何か。
  A: 最大同時接続数の備考に書かれている合言葉は、ZEBRAFINCH です。

Sources:
  - …a1_prep02-fake-workbook.xlsx (…)
```

### 4. 切り分け: xlsx 単独でも成立する（`scenario-2.md`）

「同居する txt のおかげで索引が成立しただけ」ではないことを、xlsx 1 件だけの場所に
別の索引を作って確認した（索引作成成功 / `$VECTAB` 1 行 / 同じ本文 / 検索ヒット）。

### 5. アプリの実経路でも表示が実測と一致する（`scenario-3.md`）

本番と同じ関数（`rag.add_file` → `rag_select_ai.ensure_profile` → `rag.attach_backend_status`）を
run 固有のスキーマ・バケットに向けて実行した。

```console
  prep02-fake-workbook.xlsx: backends={'vector_store': 'pending', 'select_ai': 'indexed',
                                       'opensearch': 'disabled', 'adb': 'indexed'}
```

PREP-01 の実装では、同じ状況でも拡張子だけを見て `'select_ai': 'error'` を出していた
（`runs/2026-07-30T1600_PREP-01/e2e/upload.md` の実出力）。表示が実測に追いついた。

## 何を変えたか

| 変更 | 内容 |
|---|---|
| `jetuse_core/rag.py` | `SELECT_AI_EXTENSIONS` と `_select_ai_supports()` を**削除**。`attach_backend_status` の `select_ai` は「索引に在れば `indexed` / 無ければ `pending`」だけになった（他バックエンドと同じ「実際の状態を見る」形） |
| `packages/api/tests/test_rag.py` | 恒久 `error` を固定していたテストを、実測に合わせた期待値へ差し替え |
| `specs/09-rag.md` | `[PREP-02] バックエンドごとの xlsx の扱い（実測）` を追加。「`select_ai` は xlsx を扱えない」の記述を撤回 |

表示文言は実測を超えた主張をしていない。`pending` は「索引の同期待ち」という**観測可能な状態**で
あり、「対応していません」という断定はどこにも残っていない。

### 抽出テキスト（`.xlsx.txt`）を Select AI 用に置く案 — **採らない**

原本がそのまま読めている以上、`.xlsx.txt` を併置しても得るものが無く、壊すものがある:

- **重複取り込み**: 索引の対象は場所（プレフィックス）単位なので、原本と `.xlsx.txt` の
  両方が索引に載る。同じ内容が 2 チャンクになり、検索結果と `Sources:` に同じ文書が二重に出る。
- **削除時の整合**: 1 ファイルの削除で 2 オブジェクトを消す必要が生じ、片方が残ると
  「消したはずの文書が答えに出てくる」状態になる。
- **得られるもの**: 無い。Oracle Text の抽出でシート名・行列見出しまで本文に入っており、
  アプリ側の抽出テキストが優る点は（チャンク粒度も付かないので）無い。

## 残課題（本タスクの範囲外）

- `refresh_rate`（60 分）の自動同期が、あとから置いた xlsx を拾うかは**未確認**（`SKIPPED.md` §2）。
- 大きなブック・数式・保護ブック・壊れたブックの挙動は**未確認**（`SKIPPED.md` §3）。
  「xlsx を扱える」は**小さな通常のブックで実測した**という意味である。
- `opensearch` バックエンドの xlsx は実機未確認のまま（PREP-01 から変化なし）。

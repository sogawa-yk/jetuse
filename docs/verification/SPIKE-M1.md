# SPIKE-M1 検証レポート — RAG チャンクのメタデータ（出典粒度・フィルタ検索）

日付: 2026-07-28
リージョン: ap-osaka-1（Chicago 差分は対象外＝後続タスク）
DB: 共有 loop ADB `jetuse-loop-adb` / **Oracle AI Database 26ai Enterprise Edition 23.26.3.1.0**、
スキーマ `JETUSE_SPIKE_M1`（ADB は増やさず、既存の共有 ADB にスキーマだけ足して隔離）
データ: **架空**の「サンプル在庫連携API仕様書.xlsx」を模した 10 チャンク（`spikes/spike_m1/fixtures.py`）。
うち 3 件は旧版（`current_version=False`）で、現行版と意味的に近い near-duplicate。
顧客データは一切使用していない。

検証スクリプトは `spikes/spike_m1/`。実行ログ（全文）は `runs/2026-07-28T1848_SPIKE-M1/e2e/`。
本レポートの引用はすべてその実行結果からの抜粋であり、ドキュメントの記述は根拠にしていない。
ログ中の OCID と Object Storage ネームスペースは `redact_evidence.py` で伏せてある。
本文中のサービスエンドポイントは `<region>` プレースホルダで書く（実値をドキュメントに残さないため。検証したリージョンは ap-osaka-1）。

## 結論（先に）

| | ① OCI Vector Store + file_search | ② Select AI 索引（DBMS_CLOUD_AI） | ③ ADB 自前索引（DBMS_VECTOR_CHAIN） |
|---|---|---|---|
| 任意メタデータの付与 | **可**（`attributes`・最大16キー） | **不可**（固定6キー。`$VECTAB` へ列追加で代替は可） | **可**（列でも JSON でも自由） |
| メタデータでのフィルタ検索 | **可**（`filters`: eq/and/or/gte） | 標準経路では**不可**（自前で足した列に SQL を書けば可） | **可**（SQL の WHERE） |
| 出典の粒度 | **ファイル単位**（チャンクごとに変えられない） | ファイル単位（object_name + オフセット） | **チャンク単位**（任意の列） |
| 版フィルタ 1 本の SQL | — | — | **成立**（実行結果あり） |

**当初の想定（①では属性もフィルタも持てない）は実機で覆った。** ① は属性もフィルタも動く。
差が出るのは「属性がファイル単位か、チャンク単位か」と「業務データと結合できるか」である。
詳細な比較は `docs/comparison/rag-metadata-backends.md`、採否の提案は
`docs/decisions/ADR-0019-rag-metadata-backend.md`（案）。

---

## ① OCI Vector Store + file_search

実行: `PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_a_vector_store.py`
（証跡 `e2e/method-a-vector-store.log`）
限界測定: `... spikes/spike_m1/method_a_limits.py`（証跡 `e2e/method-a-limits.log`）

### ①-a チャンク単位の属性付与 → **ファイル単位でなら可**

`vector_stores.files.create(..., attributes={...})` は受理され、保持もされる。

```
  渡そうとした attributes: {"file": "サンプル在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
   "cells": "B12:F12", "sha256": "d134954f...", "kind": "spec", "current_version": "Y"}
  attributes 付き登録 OK: c01__v2.0__current__spec.txt
```

登録後の実体（`vector_stores.files.retrieve`）:

```json
{
  "id": "file-kix-e7a5ef5a-...", "object": "vector_store.file", "status": "completed",
  "usage_bytes": 6998,
  "attributes": {
    "file": "サンプル在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
    "cells": "B12:F12", "sha256": "d134954f8ee0...", "kind": "spec", "current_version": "Y"
  },
  "chunking_strategy": null
}
```

ただし**属性はファイル単位**である。1 ファイルが 5 チャンクに割れる本文を投入すると、
5 チャンクすべてが同じ属性を返す（`e2e/method-a-limits.log` ①-L1）:

```
  このファイル由来のヒット（チャンク）数: 5
    chunk_id=3_263838af-... cells=A1:Z999 sheet=全体 text=第4章 サンプル在庫連携APIの規定その4。…
    chunk_id=4_fd1096fa-... cells=A1:Z999 sheet=全体 text=第5章 サンプル在庫連携APIの規定その5。…
    chunk_id=2_0ea41f1c-... cells=A1:Z999 sheet=全体 text=第3章 …
    chunk_id=0_48ed111a-... cells=A1:Z999 sheet=全体 text=第1章 …
    chunk_id=1_7bd23d88-... cells=A1:Z999 sheet=全体 text=第2章 …
  => 異なる attributes の種類: 1 （1 なら属性はファイル単位＝チャンクごとの cells は持てない）
```

→ セル範囲・シートをチャンクごとに変えたければ **1 チャンク = 1 ファイル**にするしかない
（10 チャンクなら 10 ファイル。本スパイクの主データセットは実際そうしている）。

### ①-b メタデータフィルタ → **可**

`vector_stores.search(..., filters=...)` と Responses の `tools[].filters` の双方で効く。
フィルタ無しでは旧版 `c08`/`c09` が上位に来るが、`current_version='Y'` で消える。

```
[フィルタ無し] c08(1.0/N) score 0.8536 → c01(2.0/Y) 0.8507 → c09(1.0/N) 0.7261 → c05(2.0/Y) 0.6890 …
[eq current_version=Y] c01 0.8507 → c05 0.6890 → c07 0.4355 → c02 0.3827 → c03 0.3680（旧版 0 件）
```

表現力（`e2e/method-a-limits.log` ①-L2）:

| フィルタ | 結果 |
|---|---|
| `eq` | OK（5 件） |
| `and`（2 条件） | OK（3 件） |
| `or`（2 条件） | OK（10 件） |
| `gte`（文字列比較） | OK（10 件） |
| `in` | **NG**。`400 invalid_value / Status Code from provider: 422 … {"type":"missing","loc":["body","filters",…,"value"],"msg":"Field required"}`（`values` 配列を解釈せず `value` を要求する） |
| 存在しないキー | OK（0 件。エラーにならず空になる＝タイポが静かに全件除外する） |

### ①-c 返却フィールド（`file_search_call.results` / annotations）

`include=["file_search_call.results"]` 有りの Responses 応答から:

```json
{
  "attributes": {"file":"…","version":"1.0","sheet":"API一覧","cells":"B12:F12",
                 "sha256":"770dee50…","kind":"spec","current_version":"N"},
  "file_id": "file-kix-5f316f74-…",
  "filename": "c08__v1.0__stale__spec.txt",
  "score": 0.85359573,
  "text": "在庫照会API GET /v1/inventory は…最大200件まで返却する。",
  "vector_store_id": "vs_kix_577f3q…",
  "additional_properties": {"vector_store_id": "vs_kix_…", "chunk_id": "0_ad585b8f-…"}
}
```

message annotations 側は**属性もスコアも本文も返らない**:

```json
{"file_id":"file-kix-5f316f74-…","filename":"c08__v1.0__stale__spec.txt","index":0,
 "type":"file_citation","valid":true,
 "additional_properties":{"chunk_id":"0_ad585b8f-…","page_numbers":null}}
```

→ 現行 `jetuse_core/chat.py:_extract_citations()` が `{file_id, filename, score}` しか返さないのは
**OCI 側の制約ではなくアプリ側の実装**である。`file_search_call.results[].attributes` と `.text`
を拾えば、出典を構造化して返せる（本スパイクは JetUse 本体を変更しないので提案のみ）。

### ①-d 属性の上限（`e2e/method-a-limits.log` ①-L4）

| 試行 | 結果 |
|---|---|
| キー 20 個 | NG `Metadata must not contain more than 16 key-value pairs.` |
| 値 600 文字 | NG `Metadata value for key 'long' exceeds max length of 512 characters.` |
| 数値型の値 | 受理 |
| 入れ子オブジェクト | NG `Metadata value … must be a string, number, or boolean.` |

### ①-e 取り込み後の属性更新 → **可**

`vector_stores.files.update(attributes=...)` で `current_version` を Y→N→Y に付け替えられた
（再取り込み・再埋め込みは不要）。

### ①-f 実機の落とし穴

Vector Store は CP が completed でも DP へ伝播するまで files 系が 404 になる。
待たずに登録すると属性の可否判定と混ざる（最初の試行で実際に混ざった）:

```
Error code: 404 - {'code': '404', 'message': '{"error":{"message":"VectorStore vs_kix_… with status Completed not found."…}}'}
```

---

## ② Select AI のベクトル索引（DBMS_CLOUD_AI.CREATE_VECTOR_INDEX）

実行: `PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_b_select_ai.py`
（証跡 `e2e/method-b-select-ai.log`）
リフレッシュ耐久: `... spikes/spike_m1/method_b_refresh_check.py`（証跡 `e2e/method-b-refresh-durability.log`）

### ②-a attributes に任意キー → **不可（明確に拒否される）**

```
CREATE_VECTOR_INDEX('JETUSE_SPIKE_M1_IDX_ARB', {"vector_db_provider": "oracle", …,
  "current_version": "Y", "kind": "spec", "sheet": "制約"})
-> 拒否（エラー全文）:
   ORA-20048: Invalid vector index attribute - current_version
   ORA-06512: at "C##CLOUD$SERVICE.DBMS_CLOUD$PDBCS_260708_0", line 2291
   ORA-06512: at "C##CLOUD$SERVICE.DBMS_CLOUD_AI", line 20726
```

`CREATE_VECTOR_INDEX` の `attributes` は**索引の設定項目**であって、
チャンクに付けるメタデータではない。索引表 `$VECTAB` の `attributes` 列に入るのは固定 6 キー:

```
列構成: CONTENT:CLOB, ATTRIBUTES:JSON, EMBEDDING:VECTOR
{
  "object_name" : "c05__v2.0__current__constraint.txt",
  "object_size" : 138,
  "last_modified" : "2026-07-28T10:37:42+00:00",
  "location_uri" : "https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/jetuse-spike-m1/o/chunks/",
  "start_offset" : 1,
  "end_offset" : 58
}
```

→ 任意メタデータを載せる唯一の手段は**オブジェクト名に埋め込むこと**（本スパイクは
`c05__v2.0__current__constraint.txt` のようにした）。文字列パースが前提になる。

### ②-b `$VECTAB` に列追加 → **可。ただしリフレッシュで欠ける**

`$VECTAB` はただの表なので `ALTER TABLE` が通り、SQL でフィルタもできる:

```
ALTER TABLE "JETUSE_SPIKE_M1_IDX$VECTAB" ADD (current_version CHAR(1)) -> OK
補完後の内訳: [('N', 3), ('Y', 7)]

[版フィルタ] SELECT JSON_VALUE(attributes,'$.object_name'), VECTOR_DISTANCE(EMBEDDING, :q, COSINE)
            FROM "JETUSE_SPIKE_M1_IDX$VECTAB" WHERE current_version = 'Y' …
  c01__v2.0__current__spec.txt | 0.2546
  c05__v2.0__current__constraint.txt | 0.3365
  c03__v2.0__current__spec.txt | 0.3822 …（旧版 0 件）
```

しかし索引はバケットから定期同期される。同期で入った新しい行に自前列の値は**入らない**:

```
リフレッシュ前: [('N', 3), ('Y', 7)]
新規オブジェクト追加: chunks/c11__v2.0__current__spec.txt（索引に未登録のものを選択）
STOP_PIPELINE -> OK / RUN_PIPELINE_ONCE -> OK / START_PIPELINE -> OK
リフレッシュ後の総行数: 11
リフレッシュ後の current_version 内訳: [('(NULL)', 1), ('N', 3), ('Y', 7)]
=> 同期で入った行の current_version は NULL: 1 件。取り込みのたびに自前の補完処理を回し続ける必要がある
```

（実機の注意: 走行中パイプラインは前景実行できず `ORA-20044: Pipeline must be in stopped state
to run in foreground.` になる。STOP → RUN_ONCE → START の順が要る。）

### ②-c Select AI の標準経路が返す出典 → **ファイル名と URL だけ**

```
在庫照会APIのレート制限は、1分あたり600リクエストである。…1回の最大取得件数は…最大1000件まで返却する。

Sources:
  - c01__v2.0__current__spec.txt (https://objectstorage.<region>.oraclecloud.com/n/<OS_NAMESPACE>/b/jetuse-spike-m1/o/chunks/c01__v2.0__current__spec.txt)
  - c05__v2.0__current__constraint.txt (…)
```

スコアもチャンク本文もオフセットも無く、応答本文末尾の**文字列**として付く
（`rag_select_ai.split_sources()` が正規表現で剥がしているのはこれ）。
フィルタを渡す口も `DBMS_CLOUD_AI.GENERATE` には無い。

---

## ③ ADB 自前索引（DBMS_VECTOR_CHAIN + VECTOR 列 + メタ列）

実行: `PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_c_own_index.py`
（証跡 `e2e/method-c-own-index.log`）

### ③-a 表定義（メタデータは列でも JSON でも自由）

```sql
CREATE TABLE SPIKE_CHUNKS (
  chunk_id        VARCHAR2(64)  PRIMARY KEY,
  doc_file        VARCHAR2(400) NOT NULL,   -- 出典: ファイル名
  doc_version     VARCHAR2(32)  NOT NULL,   -- 出典: 版
  sheet_name      VARCHAR2(128),            -- 出典: シート
  cells           VARCHAR2(64),             -- 出典: セル範囲
  sha256          VARCHAR2(64)  NOT NULL,   -- 出典: 原本ハッシュ
  kind            VARCHAR2(32)  NOT NULL,   -- 分類: spec / constraint
  current_version CHAR(1)       NOT NULL,   -- 版フラグ: Y / N
  attributes      JSON,                     -- 任意の追加メタ（スキーマレス）
  body            CLOB          NOT NULL,
  embedding       VECTOR(1024, FLOAT32)
)
```

ベクタ索引は HNSW が通った:
`CREATE VECTOR INDEX SPIKE_CHUNKS_VIDX ON SPIKE_CHUNKS(embedding)
ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95` → OK

### ③-b DB 内埋め込み（アプリ層に本文を持ち出さない）→ **可**

注意: DB 内埋め込みでも**本文は OCI Generative AI の `embedText` エンドポイントへ送られる**。
「アプリ層を経由しない」のであって「テナント外に一切出ない」ではない。

```
[最小構成] params={"provider":"ocigenai","credential_name":"JETUSE_SPIKE_M1_VCRED",
  "url":"https://inference.generativeai.<region>.oci.oraclecloud.com/20231130/actions/embedText",
  "model":"cohere.embed-multilingual-v3.0"}
  -> OK 次元数=1024
```

資格証明は **`DBMS_VECTOR_CHAIN.CREATE_CREDENTIAL` で作る必要がある**。
`DBMS_CLOUD.CREATE_CREDENTIAL` の名前を渡すと別ストアなので引けない:

```
ORA-20002: The provider returned an error - Error Code: -20003,
Error Message: ORA-20003: error retrieving credential
```

### ③-c 版フィルタ + ベクタ検索が 1 本の SQL で成立（完了条件の中核）

```sql
WITH qvec AS (SELECT DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING(:q, JSON(:p)) AS q FROM dual)
SELECT chunk_id, doc_file, doc_version, sheet_name, cells, kind, current_version,
       SUBSTR(sha256,1,12) AS sha256_head,
       JSON_VALUE(attributes,'$.source') AS attr_source,
       ROUND(VECTOR_DISTANCE(embedding,(SELECT q FROM qvec),COSINE),4) AS dist,
       SUBSTR(body,1,42) AS body_head
FROM SPIKE_CHUNKS
WHERE current_version = 'Y'
ORDER BY VECTOR_DISTANCE(embedding,(SELECT q FROM qvec),COSINE)
FETCH FIRST 5 ROWS ONLY
```

同一クエリでの対照（`e2e/method-c-own-index.log` ③-4）:

| | ヒット | 旧版(`current_version='N'`) |
|---|---|---|
| A: フィルタ無し | c01, **c08**, **c09**, c05, c03 | **2 件** `['c08','c09']` |
| B: `WHERE current_version='Y'` | c01, c05, c03, c02, c07 | **0 件** `[]` |
| C: `… AND kind='constraint'` | c05, c07, c06 | 0 件 |

```
PASS: 版フィルタ 1 本の SQL で旧版を完全排除
```

実行計画（`EXPLAIN PLAN`。実績プランは V$SESSION 権限が無く取得不可）:

```
|*  6 |     TABLE ACCESS STORAGE FULL| SPIKE_CHUNKS |
   6 - storage("CURRENT_VERSION"='Y')
       filter("CURRENT_VERSION"='Y')
```

10 行しかないためオプティマイザは**索引を使わず全件走査＋厳密検索**を選んだ。
つまりこの計測で「HNSW 索引 + フィルタ」の挙動までは確認できていない
（`WHERE` が先に効くこと自体は述語から確認できる）。大規模データでの索引利用と
再現率は後続タスクで測る必要がある。

### ③-d 出典を構造化して返す

```sql
SELECT JSON_SERIALIZE(JSON_OBJECT('chunk_id' VALUE chunk_id, 'file' VALUE doc_file,
  'version' VALUE doc_version, 'sheet' VALUE sheet_name, 'cells' VALUE cells,
  'sha256' VALUE sha256, 'kind' VALUE kind, 'current_version' VALUE current_version) PRETTY)
FROM SPIKE_CHUNKS WHERE chunk_id = 'c05'
```

```json
{
  "chunk_id" : "c05", "file" : "サンプル在庫連携API仕様書.xlsx", "version" : "2.0",
  "sheet" : "制約", "cells" : "C5:E5",
  "sha256" : "eaa8b7f54831e2e59d8959511dc639d2829f21ccbbf825f4e828f5213eeead27",
  "kind" : "constraint", "current_version" : "Y"
}
```

本文への埋め込みではなく列 / JSON として返っている。

---

## レイテンシ実測（同一クエリ・各 5 回・中央値。証跡 `e2e/latency.log`）

土俵が違うものを 1 表に混ぜないため、検索のみ／生成込みで分けた。

### 検索のみ

| 経路 | 中央値 | min–max |
|---|---|---|
| ③ 1 本 SQL（DB 内埋め込み込み・版フィルタ） | **192.7 ms** | 191.0–224.5 |
| ③ SQL のみ（埋め込み済みベクタを渡す・版フィルタ） | **16.7 ms** | 16.5–24.2 |
| ② `$VECTAB` 直接検索（自前追加列でフィルタ） | 69.3 ms | 42.7–72.9 |
| ① Vector Store `search` API（属性フィルタ付き） | 121.5 ms | 95.7–318.4 |
| （参考）埋め込み API 単体 | 84.5 ms | 37.4–106.6 |

### 生成込み

| 経路 | 中央値 | min–max |
|---|---|---|
| ① Responses + file_search | 4323.5 ms | 3388.0–4989.0 |
| ② `DBMS_CLOUD_AI.GENERATE(narrate)` | 3276.1 ms | 3230.9–3384.5 |
| ③ | 該当なし（③ は検索のみを担い、生成は別の LLM 呼び出し） |

読み方の注意:
- **3 方式とも同じ 10 チャンクを見ている状態で測っている**。計測前に各方式から chunk_id を取り出して
  `['c01'…'c10']` が一致することを実測で確認し（③ 表 / ② `$VECTAB` / ① ストアの属性。
  一覧はページングし、重複レコードも不一致として弾く）、揃わなければ計測を中止する。
  実測値: 3 方式とも実レコード 10 件 / 異なる chunk_id 10 件。
- ③ の「1 本 SQL 192.7 ms」には DB からの埋め込み API 呼び出しが含まれる。
  アプリ側で埋め込み済みベクタを渡すなら SQL は 16.7 ms。埋め込み 84.5 ms を足すと
  約 101 ms で、① の 121.5 ms とほぼ同じ。**この規模では検索の速さは方式選択の決め手にならない**
  （差が出るのは表現力と粒度の側）。
- 数値は実行ごとに揺れる（同じ条件でも ① の検索は 96〜318 ms、生成込みは 3.4〜5.0 秒に分布した）。
  桁で読むこと。有効数字を追わない。
- 10 チャンクという極小データでの数字である。件数が増えたときの傾きは本スパイクでは測っていない。
- ローカル macOS → 大阪リージョンの往復を含む。配備済み Container Instance からの実測ではない。

---

## 未実施・限界（無言スキップしない）

- 大規模データ（数万〜数十万チャンク）での再現率・索引利用は未計測。
  10 行では ③ のオプティマイザが索引を使わず全件走査を選ぶため、
  「フィルタ + HNSW/IVF」の組み合わせ挙動は本スパイクでは確認できていない。
- Chicago リージョンでの再確認は対象外（tasks/SPIKE-M1.md 非ゴール）。
- ③ の VPD / 業務表との JOIN は「SQL なので書ける」以上の実証をしていない
  （架空チャンクのみで業務表を作らなかったため）。ADR の根拠は SQL であることまでに留める。
- 詳細は `runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md`。

## 検証中に起こした事故（記録）

安全ガードの否定シナリオ（`guard_checks.py` の G3）の初版が、台帳ゲートの無い
`teardown.py --yes` を空台帳で実行し、**検証用スキーマ `JETUSE_SPIKE_M1` を実際に削除した**。
共有 ADB の他スキーマ・他リソースへの波及は無い（対象がスキーマ名で限定されていたため）。
DB 側にも台帳ゲートを入れたうえでスキーマを作り直し、①②③ の証跡はすべて取り直している。
詳細は `runs/2026-07-28T1848_SPIKE-M1/e2e/scenario-4.md`。

## 検証用リソースの片付け

`spikes/spike_m1/teardown.py --yes` で以下を削除する（既定は dry-run）:
ADB のベクトル索引 / プロファイル / 表 / 資格証明 / スキーマ `JETUSE_SPIKE_M1` と ACL、
Object Storage バケット `jetuse-spike-m1`、Vector Store `jetuse-spike-m1-vs` とその files、
GenAI プロジェクト `jetuse-spike-m1-project`。

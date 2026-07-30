# RAGM-02 検証レポート — ADB 自前索引のスケール実測と `adb` バックエンド

日付: 2026-07-30 / リージョン: ap-osaka-1
DB: 共有 loop ADB `jetuse-loop-adb`（Oracle AI Database 26ai Enterprise Edition 23.26.3.1.0）
スキーマ: **run 固有名 `JETUSE_RAGM02_<乱数>`**（ADB は増やさず、共有 loop ADB 内で
並行タスク RAGM-01 とスキーマで隔離。固定名をやめた理由は ⑦）
データ: **架空**の業務文書チャンク（`spikes/ragm02/fixtures.py`）。顧客データは使用していない。

検証スクリプトは `spikes/ragm02/`、実行ログは `runs/2026-07-30T0025_RAGM-02/e2e/`。
本レポートの数値はすべてその実行結果からの抜粋であり、ドキュメントの記述は根拠にしていない。
本レポートの数値は**添付証跡（同一 run・単一の検証用スキーマ）の JSON / ログと一致する**。
50,000 チャンクの投入からやり直した計測は開発中に 3 回行っており（検索 SQL の確定前 / 確定後 /
run 固有スキーマへの変更後）、**掲載しているのは最後の 1 回**である。回によって変わった点
（素の表での実行計画の揺れ）は下記 ② に明記する。

## 結論（先に）

1. **50,000 チャンク規模でベクタ索引（HNSW）は作れる**（57 秒）。SPIKE-M1 の 10 行では
   確認できなかった「索引が実際に使われる状態」に到達した。
2. **メタデータ絞り込み付きのベクタ検索でも索引は使える**。ただし**素の表では計画が揺れる**
   （回によって `HNSW SCAN IN-FILTER` と `TABLE ACCESS STORAGE FULL` の両方を観測）。
   **フィルタ列に B木索引を張ると、観測したすべての回で索引が使われた**。実装どおりの索引
   （`owner_sub` 先頭）と実装の SQL で測ると `HNSW SCAN PRE-FILTER`（メタデータ索引で前絞り
   → 近似検索）になる。
   → **この結果を実装に反映**し、`rag_adb.ensure_indexes()` が
   `(owner_sub, file_id)` / `(owner_sub, current_version, kind)` / `(owner_sub, doc_file)` を作る。
3. **実再現率は索引が使われるすべての構成で recall@10 = 1.00**（同条件の厳密検索を正解とした一致率）。
   クエリ側 `TARGET ACCURACY` を 70 まで落とすと平均 0.99・最低 0.80。既定の 95 は据え置く。
4. **索引が無くても結果は正しい**。同じ `FETCH APPROX FIRST` の SQL は、ベクタ索引を落とすと
   厳密検索へ落ちて結果が一致した（実測）。索引作成に失敗しても取り込みを止めない設計の根拠。
5. この規模では**速度は方式選択の決め手にならない**（実装どおりの形で厳密 18〜67 ms / 近似 18〜68 ms）。
   差が出るのは表現力と出典粒度の側（ADR-0020 の判断どおり）。
6. **埋め込みはクライアント側に決めた**。DB 内埋め込み（`UTL_TO_EMBEDDING`）は
   `OCI$RESOURCE_PRINCIPAL` では通らず（ORA-24247）、API キーの資格証明を DB に作る必要があり、
   それは ADR-0021 が廃止した経路のため（下記 ④）。

---

## ① 規模と索引（`e2e/scale/scale-check.log`）

| | 実測 |
|---|---|
| 行数 | 50,000（現行版 33,333 / 旧版 16,667） |
| 次元 | 1024（`cohere.embed-multilingual-v3.0`） |
| 埋め込み（クライアント側・4 並列） | 約 2 分（`scale-check.log`） |
| 投入（500 行ずつ executemany） | 約 40 秒（`scale-check.log`） |
| `CREATE VECTOR INDEX ... ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95` | **成功・57.0 秒**（IVF へのフォールバックは不要だった） |

表定義は本実装のマイグレーション `017_rag_adb.sql` から読み込んで表名だけ差し替えている
（検証用に別の DDL を書き起こすと「測った表」と「実装した表」がずれるため）。

## ② メタデータ WHERE と実行計画（本タスクの中心）

recall@10 は**同条件・同じ SQL の厳密検索（`FETCH FIRST`）を正解とした一致率**。クエリは 20 本。

### ラウンド3（結論の根拠）: **実装が発行する SQL そのもの**で測る

検索 SQL は `jetuse_core.rag_adb.search_sql()`、フィルタは `rag_adb.build_where()`、索引は
`rag_adb.BTREE_INDEXES` を**そのまま**使い、表名だけ検証表に差し替えた（`scale-appshape.log`）。
測った SQL と動く SQL がずれていれば、この検証は実装の根拠にならない。

| アプリの検索条件 | 実行計画 | recall@10（平均/最低） | 近似 / 厳密（中央値） |
|---|---|---|---|
| `owner_sub` のみ | **HNSW SCAN PRE-FILTER**（owner 索引で前絞り） | 1.00 / 1.00 | 68.4 / 55.5 ms |
| `+ current_version='Y'` | **HNSW SCAN PRE-FILTER**（meta 索引で前絞り） | 1.00 / 1.00 | 52.1 / 66.7 ms |
| `+ kind='constraint'` | **HNSW SCAN PRE-FILTER**（meta 索引を RANGE SCAN） | 1.00 / 1.00 | 33.7 / 41.5 ms |
| `+ doc_file` 指定（133 行 / 0.27%） | B木 `INDEX RANGE SCAN` + 厳密検索 | 1.00 / 1.00 | 18.0 / 17.9 ms |

最後の行はベクタ索引を使わないが、**それでよい**。133 行に対しては B木で絞って厳密に測るほうが
速く（18.0 ms）、結果も正確。オプティマイザの判断は妥当である。
また**ベクタ索引を落とした状態**で同じ SQL を流すと、厳密検索と結果が完全に一致した（5 クエリ）
＝索引が作れなくても結果は正しい（`appshape.json` の `without_vector_index`）。

### ラウンド1・2: 索引構成を変えたときの計画（`scale-check.log` / `scale-filters.log`）

| 表の状態 | フィルタ無し | 版フィルタ | 版+分類 | 版+ファイル(0.27%) |
|---|---|---|---|---|
| 素（メタデータ索引なし・統計なし）**今回の計測** | HNSW | HNSW IN-FILTER | HNSW IN-FILTER | FULL SCAN |
| 素（同上）**別の回に観測** | HNSW | **FULL SCAN** | **FULL SCAN** | FULL SCAN |
| `DBMS_STATS` 採取後 | HNSW | HNSW IN-FILTER | HNSW IN-FILTER | FULL SCAN |
| フィルタ列に B木索引 | HNSW | HNSW IN-FILTER | HNSW IN-FILTER | B木 RANGE SCAN |
| 実装どおり（B木 + `owner_sub` 込み・ラウンド3） | **HNSW PRE-FILTER** | **HNSW PRE-FILTER** | **HNSW PRE-FILTER** | B木 RANGE SCAN |

**素の表では同じ条件で計画が揺れる**。50,000 行を投入した直後という同じ手順でも、
**別の回には同じ条件が全件走査になった**（統計の状態＝ADB のオンライン統計収集のタイミングによる）。
一方、**メタデータ列に索引がある構成では、観測したすべての回で索引が使われた**
（B木を当てたラウンド2 は IN-FILTER、実装どおりの `owner_sub` 込みのラウンド3 は PRE-FILTER）。
素の表の揺れは再現性が無いため、**索引を張って計画を固定するのが実装の結論**であり、
「索引を作れば効く」ではなく「**絞り込みに使う列にも索引が要る**」が持ち帰りである。

`/*+ VECTOR_INDEX_SCAN */` ヒントでの強制も試した（`scale-filters.log` C 節）。版・分類フィルタでは
索引を使わせられ recall は 1.00。高選択度の条件だけはヒントでも B木経路が選ばれた。

参考（ラウンド2 B 節の PRE-FILTER 計画。メタデータ索引と HNSW の写像表を突き合わせている）:

```
|  5 |      VECTOR INDEX HNSW SCAN PRE-FILTER| SCALE_CHUNKS_VIDX
|  6 |       VIEW                            | VW_HPJ_...
|* 7 |        HASH JOIN OUTER
|* 8 |         INDEX STORAGE FAST FULL SCAN  | (メタデータ索引)
|  9 |         TABLE ACCESS STORAGE FULL     | VECTOR$..._HNSW_ROWID_VID_MAP
   8 - storage("CURRENT_VERSION"='Y') / filter("CURRENT_VERSION"='Y')
```

## ③ `TARGET ACCURACY` と実再現率（索引が実際に使われる経路で測る）

クエリ側の `WITH TARGET ACCURACY` を振った（`scale-filters.log` D 節・20 クエリ・フィルタ無し）。

| 指定 | recall@10 平均 | 最低 | 近似（中央値） |
|---|---|---|---|
| 70 | 0.99 | **0.80** | 34.7 ms |
| 80 | 1.00 | 1.00 | 35.8 ms |
| 90 | 1.00 | 1.00 | 38.2 ms |
| 95（索引の既定） | 1.00 | 1.00 | 35.3 ms |
| 100 | 1.00 | 1.00 | 41.9 ms |

→ 80 以上で取りこぼしが無く、下げてもレイテンシは縮まらない。**`TARGET ACCURACY 95` を
下げる理由が無い**のでそのままにした。

## ④ 埋め込みは DB 内かクライアント側か（決定と根拠）

| | クライアント側（採用） | DB 内 `UTL_TO_EMBEDDING` |
|---|---|---|
| 資格情報 | **DB に持たない**（アプリの `oci_auth` を使う。ADR-0021 と整合） | DB 内に必要 |
| `OCI$RESOURCE_PRINCIPAL` で動くか | 該当なし | **不可**（下記 ORA-24247） |
| 必要な追加作業 | 無し（`jetuse_core.embeddings` を再利用） | `DBMS_VECTOR_CHAIN.CREATE_CREDENTIAL` で API キーの資格証明を作る |
| バッチ | 96 件ずつまとめて呼べる | 1 行ずつ SQL の中で呼ぶ |
| 本文の送出先 | OCI GenAI `embedText` | 同じ（「テナント外に出ない」ではない） |

RP での実測（ACL に `connect` / `resolve` / `http` を付与してもなお不可）:

```
ORA-20000: Oracle Text error:
DRG-50857: oracle error in dbms_vector_chain.utl_to_embedding(clob)
ORA-20002: The provider returned an error - Error Code: -20000,
  Error Message: ORA-20000: ORA-24247: Network access denied by access control list (ACL)
```

`DBMS_VECTOR_CHAIN` の資格証明は `DBMS_CLOUD` のものを引けない（SPIKE-M1 で ORA-20003 実測）。
つまり DB 内埋め込みを採るなら **API キーを DB に焼く経路の復活**が要る。それは RP-01 / ADR-0021 で
廃止した方向なので、**クライアント側を既定にし、DB 内埋め込みの実装は入れなかった**
（使えない選択肢を設定で残さない）。将来 DB 内埋め込みが要るなら、資格情報方針の判断が先に要る。

## ⑤ 実装への反映

- `packages/api/jetuse_core/rag_adb.py`（新規・既存 3 バックエンドは未変更）
  - 検索は `FETCH APPROX FIRST`（②の計画を得る形）。`search_sql()` を公開し、**検証スクリプトが
    同じ関数から SQL を得る**ようにした（測った SQL と動く SQL を構造的に一致させる）。
  - `ensure_indexes()` が B木 3 本 + ベクタ索引（HNSW → 不可なら IVF）を冪等に作る。
    **マイグレーションに索引を書かない**のは、Oracle の DDL が暗黙コミットで、1 ファイルに
    複数 DDL を並べると途中失敗時に「表はあるが migration 未記録」で再実行不能になるため。
  - 上限を超える 1 行は文字オフセット付き（`L12c1-800`）で分割する。分割しないと埋め込み側の
    2000 文字切り詰めと本文が食い違い、生成時のプロンプトも破裂する。改行も長さに数える。
  - 版は**文書レジストリ `rag_adb_docs`（018）の行を作ってからロックして**採番する。
    行を先に作るので、同名ファイルの**初回同時取り込みも直列化**される（実 ADB の 2 接続で確認
    = E2E シナリオ 7。版 1.0 / 2.0 に分かれ、現行版は 1 つ）。
  - 取り込み状態は **`rag_adb_ingest`（019・`file_id` 単位）** に持つ。文書単位に持たせると
    同名ファイルの後続取り込みが前の失敗を上書きし、失敗が `pending` に戻って見える。
    未対応形式・本文を取り出せない（0 チャンク）・例外を `error` として出す。
  - **対応形式を明示**（`.txt` / `.md` / `.pdf`）。何でも UTF-8 として読むと DOCX / 画像が
    文字化けした本文として `indexed` になるため、未対応は取り込まず `error` にする。
  - 文書キーはバイト長で切り、切ったときは原名のハッシュを付ける（`VARCHAR2(400)` が
    BYTE セマンティクスでも収まり、先頭が同じ別ファイルが同一文書に統合されない）。
  - 現行版のファイルを削除したら、**残っている最大版を現行へ戻す**（同一トランザクション）。
    戻さないと旧版が全部 `N` のまま残り、既定の検索から永久に消える。
- `migrations/017_rag_adb.sql`: **CREATE TABLE 1 文だけ**（本文 + メタ列 + `JSON` + `VECTOR(1024, FLOAT32)`）。
  `018_rag_adb_docs.sql` も 1 文（文書レジストリ）
- 削除は RAG 台帳（`rag_files`）と**同一トランザクション**（`rag.py:_delete_row`）。
  「API は削除成功なのにチャンクが残って以後の回答に混ざる」を構造的に潰す。
- `rag_backend='adb'` を API（`ChatRequest`）とバックエンド状態（`backends.adb`）に追加

## ⑥ 未実施・限界（無言スキップしない）

- 測ったのは **50,000 チャンク**。数十万規模は測っていない（HNSW の構築時間とメモリは行数に対して
  線形とは限らないため、10 倍規模で同じ結論になる保証はない）。
- recall は **20 クエリ / top-10** の平均。クエリ集合は架空チャンクの言い換えで、
  実文書・実問い合わせの分布ではない。
- 実績プラン（`DBMS_XPLAN.DISPLAY_CURSOR`）は `V$SESSION` 権限が無く取得できない。
  本レポートの計画はすべて `EXPLAIN PLAN`（推定）である。
- **VPD / Data Redaction は検証していない**（別タスク。ADR-0020 の未解決事項のまま）。
  「行レベル制御ができる」と資料に書ける段階ではない。
- 更新負荷・索引の再構築（大量の再取り込み）は測っていない。同時取り込みは
  **2 接続の同名ファイル競合のみ**確認した（シナリオ 7）。多数同時・多文書の負荷は別。
- 1 利用者（`owner_sub` 単一値）での測定。多数利用者が同居したときの選択度は別。
- 取り込み失敗時の**自動**リトライは未実装（失敗は `backends.adb` が `error` になり、
  同じファイルを上げ直せば取り込まれる = 版が上がる）。恒久的な再試行ジョブは後続。

## ⑦ 検証用リソースの片付け

`spikes/ragm02/teardown.py --yes` は、台帳（`RAGM02_HOME/ledger.json`）と実機が
**USER_ID / 作成時刻 / この run 固有のマーカー**の 3 点で一致したときだけ削除する。
照合は**開始時と `DROP USER` の直前の 2 回**行う。`setup_schema.py` の既存再利用も、
最初の DDL より前に同じ 3 点を照合する（不一致なら権限を 1 つも変えずに中止）。
否定シナリオ 6 件で「非ゼロ終了・リソース無傷」を確認済み（`e2e/guard.md`）。

否定シナリオは**実削除（`--yes`）を実際に試させている**（ガードが壊れていても、対象は
この run が作ったスキーマなので他タスクへ届かない）。片付けに成功したときだけ、ローカルの
mTLS ウォレット・スキーマのパスワード・台帳・スキーマ名も削除する。

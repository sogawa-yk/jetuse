# SYNC-01 検証レポート — main → dev 同期（RAGM-01 / RAGM-02 × SP2-02）

- 対象: `feat/SYNC-01`（base `origin/dev`）に `origin/main`（`96e7311`）を取り込む
- merge-base: `d58a341`
- 衝突: 7 ファイル 19 箇所（約 850 行）。`--ours` / `--theirs` は使っていない
- 結果: `pytest packages/api/tests` 全件パス / `ruff check packages/api` クリーン /
  実 ADB E2E 4 本 PASS（`runs/2026-07-30T1303_SYNC-01/e2e/`）
- Codex レビュー: review-1 で **blocker 1 / major 4** → 3 件修正 → review-2 で **PASS**
  （blocker 0 / major 4 = 非 blocker の助言。下記「Codex レビュー」）

## なぜ機械的に解けなかったか

dev(SP2-02) は「デモ単位のデータ分離＝信頼境界」のために `rag.py` の**外部名と削除の順序**を
作り替えた（予約 ledger・不透明キー・外部先行削除）。main(RAGM) は同じ関数群に
**メタデータ属性**と **ADB 自前索引**を足した。両者が同じ関数の同じ行を触っているので、
どちらかを採ると相手の機能が落ちる。

---

## 1. `packages/api/jetuse_core/rag.py`（8 箇所）

### 1-1. import

- **dev**: `demo_targets` / `rag_ledger` / `demo_lease` / `owner_keys` / ledger 例外
- **main**: `hashlib` / `rag_metadata`
- **統合**: 両方。`from . import demo_targets, rag_ledger, rag_metadata`

### 1-2. 定数・例外

- **dev**: `MAX_FILENAME_CHARS = 400` と `BoxLimitExceededError` / `ExternalDeleteError`
- **main**: `_fit()`（バイト境界の切り詰め）
- **統合**: 両方残す。役割が違う — `MAX_FILENAME_CHARS` は**入口の拒否**（422）、
  `_fit()` は**列に収めるための最終防衛**。

### 1-3. `_insert_file` → `_insert_file_confirmed`

- **dev**: `rag_files` INSERT と ledger の `confirmed` を同一 Tx で確定したかった
- **main**: 日本語ファイル名で ORA-12899 を出さないようバイト境界で切りたかった
- **統合**: dev の関数の中で `filename[:MAX_FILENAME_CHARS]` を **`_fit(filename)` に置換**。
  文字数切りには戻していない（指示どおり）。実 ADB で列が `VARCHAR2(400 CHAR)` であることを
  確認済み（E2E シナリオ1）なので、400 バイト切りは保守的側に倒れて必ず収まる。
- **ただし副作用があった**: ルートは 400 **文字**で受理し `_fit` は 400 **バイト**で切るため、
  マルチバイト名では拡張子まで落ちる。ここから ext を導いていた削除経路が壊れていた
  （blocker F-001 / 下記参照）。ext は ledger を正本にして解決した。

### 1-4. `_delete_row()` の廃止と移設 ★最重要

- **dev**: `_delete_row()` を**廃止**し、行削除を `delete_file()` の締めの Tx へ移した
  （`rag_files` と `rag_file_ledger` を同一 Tx で消す。別 Tx だと枠が恒久漏れする）
- **main**: `_delete_row()` に `FOR UPDATE` と `rag_adb.delete_chunks(cur, ...)` を足し、
  台帳行とチャンクを同一 Tx で消したかった
- **統合**: main の意図を**移設先へ持っていった**。`delete_file()` の締めの Tx を
  `SELECT ... FOR UPDATE` → `rag_adb.delete_chunks(cur, ...)` → `DELETE rag_files` →
  `DELETE rag_file_ledger` → `commit` の順にした。
  - `FOR UPDATE` は `rag_adb._ingest()` が同じ `rag_files` 行をロックするため、
    取り込み中なら完了を待つ（待たないと削除後にチャンクが commit される）。
  - **可用性チェックは挟んでいない**（main のコメントごと移した）。`enabled()` は別接続で
    瞬断を False に丸めるため、削除のスキップ条件にすると「API は削除成功なのにチャンクが残る」を
    再現してしまう。
  - 実 ADB で 2a（同一 Tx で全部消える）/ 2b（失敗時は全部戻る）を確認（E2E シナリオ2）。

### 1-5. デモ箱の後始末（衝突していないが両立が必要）

`demo_cleanup._cleanup_rag.step_files` も `_delete_row()` の子孫。ここに同じ 1 行を入れないと、
**箱ごと消したときだけ**チャンクが残る。`SELECT ... FOR UPDATE` → `delete_chunks` → 行削除 を
同一 Tx で行うようにした。`test_demo_cleanup.py` で「消えた台帳行の集合 == 消えたチャンクの集合」を固定。

### 1-6. 原本オブジェクトのキー

- **dev**: `_backup_original` / `_delete_original` を廃止し、`original_object_name(owner, rid, ext)`
  の**不透明キー**へ（キーからファイル名を推測できないことが SP2-02 の信頼境界の一部）
- **main**: 同じ 2 関数のキーを `_fit(filename)` にして、400 バイト超の名前で削除時にキーが
  一致せず原本が消し残るのを防ぎたかった
- **統合**: **dev の不透明キーを採用**（オーケストレータ判断）。不透明キーならファイル名が
  キーに入らないので、main が塞ごうとした桁あふれ問題そのものが起きない。
  `_fit()` は台帳の `filename` 列に対しては引き続き使う（1-3）。
  - **落ちた機能はない**が、1 点だけ記録: `_delete_original_legacy()` は旧命名
    `rag/<owner>/<file_id>_<filename>` を**原名のまま**消す。main を配備した環境が
    `_fit` 済みの名前で書いた原本は、この経路では消えない。dev 系統には `_fit` 済みキーの
    原本を書いたコードが存在したことがない（dev は不透明キー、それ以前は原名）ため
    実害は無いと判断した。→ 「残課題」に記載。

### 1-7. `add_file()`

- **dev**: `lease` 引数・予約 ledger・不透明 filename・各失敗点での収束削除
- **main**: `attributes` 引数・`build_attributes()`・`files.create(attributes=)`・ADB 取り込み
- **統合**: 署名は `add_file(owner, filename, content, attributes=None, *, lease=None)`。
  - `build_attributes()` は**あらゆる副作用より前**（`rag_ledger.reserve` と
    `demo_targets.record_target` と OCI 呼び出しの前）に置いた。不正な属性で 422 になるときに
    箱のファイル数枠を消費しないため（main の「送信前に検証」と dev の quota gate の両立）。
    `test_rag_add_file_retry.py` で `ledger.reserved == []` を固定。
  - ADB 取り込みブロックは OpenSearch 取り込みの後。`file_id` は dev の**予約 ID `rid`** に
    読み替えた（ledger・`rag_files`・外部名・チャンクを単一 ID で突合する SP2-02 の前提）。
  - `lease` は**キーワード専用**（`*` 区切り）。第4位置は main が位置で渡す `attributes` に
    譲ったので、位置引数のまま両立させると `add_file(o, n, c, lease)` が DemoLease を
    attributes として展開して壊れる（F-002）。実呼び出しは全て `lease=` キーワードで確認済み。
  - **補完関係の発見**: dev は OCI Files 側の filename を不透明キーにするので、
    **原名が外部に残るのは `attributes["file"]` だけ**になった。RAGM-01 の属性は
    SP2-02 の不透明化と衝突するどころか、出典表示を成立させる唯一の経路になっている。
    この性質をテストで固定した（`external_filename != "spec.md"` かつ `attrs["file"] == "spec.md"`）。

### 1-8. `attach_backend_status()`

- **dev**: 変更なし / **main**: `adb` バッジを追加 → **main のまま**

---

## 2. `packages/api/service/routes/rag.py`（5 箇所）

| 箇所 | dev | main | 統合 |
|---|---|---|---|
| import | `demo_lease` / `owner_keys` | `json` / `Form` / `rag_metadata` | 両方 |
| `_rag_call` | 例外群 | `MetadataError → 422`（RAGM-01 レビュー F-002） | main の分岐を先頭に足す |
| `upload_file_response` 署名 | `demo_id` | `attributes` | `(ns, file, demo_id=None, attributes=None)` |
| 入口検証 | ファイル名長 400 → 422 | `_parse_attributes`（read 前） | 拡張子 → 名前長 → 属性 → read |
| `work()` | リース保持で `add_file` | `add_file(..., attrs)` | 両方（`add_file(ns, name, content, attrs, lease=lease)`） |

`demos.py` は `upload_file_response(..., demo_id=ctx.demo_id)` とキーワード呼びなので無変更で通る。
user 単位ルートは `attributes=attributes` のキーワード呼びに直した。

**1 点の挙動変更**: main のテストは 520 文字のファイル名で「属性側の 512 文字上限 → 422」を
見ていたが、dev の 400 文字ガードが**先に**弾くようになった。**契約（422 であって 500 でない・
OCI を呼ばない）は保たれている**ので、厳しい方の 400 を正とした。到達しなくなった
`_rag_call` の `MetadataError → 422` 正規化は防御的に残し、**直接呼ぶ単体テストを足して**
RAGM-01 レビュー F-002 の正規化が無検証にならないようにした（`test_rag_call_normalizes_metadata_error_to_422`）。

## 3. `packages/api/service/routes/chat.py`（1 箇所）

- **dev**: RAG 分岐の前に `owner_key_gate()`（未分類 owner が Select AI の永続資産を作るのを塞ぐ）
- **main**: `NON_RESPONSES_RAG_BACKENDS`（adb 追加）・`rag_filters` の 3 ガード・
  `agent and rag` ガードを RAG ディスパッチより前へ移動
- **統合**: 全部残す。`owner_key_gate()` は main の分岐の**中**（`if req.rag and ... in
  NON_RESPONSES_RAG_BACKENDS:` の先頭）へ移した＝ストリーム開始前という dev の意図を保つ。
- 既知の重複ガード（`agent and rag cannot be combined` が 2 箇所）は**触っていない**（指示どおり）。

## 4. `packages/api/service/schemas.py`（1 箇所）

import を合流（`ConfigDict` + `field_validator` / `rag_metadata` + `tts`）。
`rag_backend` の `"adb"`・`rag_filters`・validator（main）と Demo/Builder スキーマ群（dev）は
git が自動合流済み。

## 5. テスト（4 箇所 + 追随修正）

| ファイル | 統合 |
|---|---|
| `test_demo_routes.py` | fake を `add_file(..., attributes=None, lease=None)` に（両側の引数を併存） |
| `test_rag.py` | 両側の追加テストを**そのまま併存**（append 同士の衝突）。上記の 1 点だけ挙動に合わせて改訂 |
| `test_rag_add_file_retry.py` | dev が落としていた `ORA-02291` の assertion は **main 側を採って復活**。main の RAGM-01 テスト 2 本を dev の `ledger` fixture の作法へ移植 |
| `test_rag_adb.py`（main 新規） | `_delete_row` を対象にした 5 本を `delete_file` の締めの Tx 観察へ移植。`_stub_upload` を予約 ledger 経路へ |
| `test_demo_cleanup.py` | 箱の後始末にチャンク削除が入ったので fake に `fetchone` を追加＋集合一致の assertion |
| `test_box_limits.py` | fake `add_file` の署名追随 |

---

## マイグレーション番号の衝突（017〜019）

dev が `017_demos_v2` / `018_demos_idx_owner` / `019_demos_idx_visibility`、
main が `017_rag_adb` / `018_rag_adb_docs` / `019_rag_adb_ingest` を同じ番号で追加した。

**リネームしない**という判断。`migrate.py` は `version = f.stem`（ファイル名）を
`schema_migrations` の主キーにしているので両者は別行として共存し、
適用順（`sorted(glob)`）でも依存関係が無い（demos の ALTER / rag_adb_* の CREATE）。
逆にリネームすると版キーが変わり、main を配備済みの環境が**同じ DDL を新しい版キーで再適用**して
ORA-00955 で止まる（既存の表に対して版キーが違うので「未適用」に見える）。

実 ADB で 30 件が交互に適用され、再適用が no-op であることを確認済み（E2E シナリオ1）。
併せて main の 3 件を `_EXPECTED_POST` に登録し、「DDL 済み・version 未記録」からの復帰も
実機で確認した（E2E シナリオ4 / Codex F-004）。

---

## Codex レビューで見つかった統合の欠陥（review-1 / 修正済み）

統合直後の実装を Codex が採点し、blocker 1・major 4 を返した（`runs/.../reviews/review-1.json`）。
**いずれも「片側だけ見ていれば起きなかった、接続点の欠陥」**で、同期作業の本質的な難所だった。

### F-001（blocker・修正済み）原本を消し残したまま「削除成功」を返す

- 症状: 400 文字のマルチバイト名（ルートは受理）で、upload は `<rid>.md` を put するのに
  delete は `<rid>.bin` を消しにいく。Object Storage の 404 は成功扱いなので、
  **原本を残したまま** `rag_files` / `rag_file_ledger` を消して `True` を返す（追跡不能な残存）。
- 原因: 「ルートは 400 **文字**で受理」（dev）と「`_fit` は 400 **バイト**で切る」（main）の
  組み合わせで拡張子が落ちる。削除経路が切り詰め後の `rag_files.filename` から ext を導いていた。
- 修正: ext は**予約時に ledger へ記録した値を正本**にした（`rag_ledger.reconcile` が既に
  ext を正本として使っており、それに揃えただけ）。legacy 行のみ filename から導出。
  併せて `_ledger_locator()` を廃し、ledger 行を 1 回引いて locator と ext の両方を使う。
- 検証: 単体 `test_delete_uses_ledger_ext_not_truncated_filename`（RED→GREEN）と
  実 ADB の E2E シナリオ3（実物の `delete_file()` でキー一致を確認）。

### F-002（major・修正済み）`add_file` の第4位置引数の非互換

`lease`（dev の第4位置）を `attributes`（main の第4位置）で置き換えていた。
`lease` を**キーワード専用**にして両立させた。

### F-004（major・修正済み）main の 017〜019 に再実行耐性が無い

`migrate.py` の `_EXPECTED_POST` に登録し、実 ADB で「DDL 済み・version 未記録」からの
復帰を確認（E2E シナリオ4）。期待形が実 DDL と食い違うと**マイグレーションを止めてしまう**ため、
登録した 3 件すべてを実オブジェクトと照合してから採用している。

### review-1 F-003（major・未対応 / residual）ローリング配備中のファイルが永久 pending

`rag_adb.availability()` が ABSENT（表 0 件）を返す間に受理したアップロードは、
取り込みも失敗記録もされずに `backends.adb` が pending のまま残る。移行完了後も backfill されない。

**本タスクでは直さない。** これは main(RAGM-02) が単独で持ち込んだ挙動で、
統合によって生じた欠陥ではない（`add_file` の当該ブロックは main のまま `rid` に読み替えただけ）。
同期に機能追加を混ぜない方針に従い residual とする。→ **別タスクでの起票を推奨**。

### F-005（major・対応済み）E2E が実経路を通っていない

指摘どおり当初のシナリオ2 はトランザクション断片の直接実行だった。
実物の `rag.delete_file()` を通すシナリオ3 と、再実行耐性のシナリオ4 を追加した。
Object Storage の実往復だけは `RAG_BUCKET` 未設定のため未実施（シナリオ3 に明記）。

## review-2（PASS）の残 major = residual

blocker 0 で PASS。残った major 4 件は**非 blocker の助言**として記録する（loop-protocol 手順5.5）。

| id | file:line | 内容 | 扱い |
|---|---|---|---|
| F-001 | `rag.py:545` | `add_file(o,n,c,lease)` の**位置呼び出し**は依然壊れる（`lease` をキーワード専用にしても、第4位置は `attributes`） | 実呼び出しを全数確認し**該当ゼロ**（`builder_data.py` / `routes/rag.py` は `lease=`）。互換シムは存在しない呼び出しのための複雑さなので入れない |
| F-002 | `rag.py:673` | ローリング配備中（表 0 件）に受理したファイルが永久 pending | **main 由来**の既存挙動。同期に機能追加を混ぜない方針で residual。別タスク推奨 |
| F-003 | `rag.py:162` | 列が `VARCHAR2(400 CHAR)` なのに `_fit()` が **400 バイト**で切る → 日本語名が約 133 文字で黙って欠落し、一覧・引用のファイル名も切り詰まる | **オーケストレータの明示指示**（「`_fit()` を使う・文字数切りに戻さない」）に該当。独断で変えず**判断を仰ぐ**（下記） |
| F-004 | `migrate.py:50` | `017_rag_adb` の期待事後条件に `ATTRIBUTES JSON` / `EMBEDDING VECTOR` 列と NUMBER の precision/scale が無い＝異形の同名表を「適用済み」と記録しうる | 部分検証は意図的（型表現の揺れで誤停止するほうが害が大きい）。強化は別タスクで可 |

### F-003 は人間の判断が要る

`_fit()` は main が **BYTE セマンティクスの列**を前提に足した防御。dev は migration 024 で
`rag_files.filename` を **`VARCHAR2(400 CHAR)`** に移行済み（E2E シナリオ1 で実測）。
ルートは 400 **文字**で受理するので、**この列に切り詰めは本来不要**。
現状は「入る値を、入らないと思って切っている」状態。

- 影響: 400 文字級の日本語ファイル名が一覧表示と `resolve_citation_filenames` の出典名で
  約 133 文字に欠落する（データ損失ではなく表示の劣化。削除の整合は ext を ledger 正本に
  したので F-001 修正で担保済み）。
- 選択肢: (a) 現状維持（指示どおり `_fit(400)`）/ (b) `_fit(filename, 1600)` にして
  400 文字 × 最大 4 バイトを吸収 / (c) 400 CHAR 前提で切り詰め自体を外す。
- **本タスクでは変更していない。** 同期に機能変更を混ぜない方針と、明示指示の尊重による。

## 残課題（本タスクでは触らない）

1. **F-002（review-2）**: ローリング配備中に受理したファイルが永久 pending。別タスク推奨。
2. **`_delete_original_legacy()` は `_fit` 済みの旧キーを消さない**（1-6 参照）。
   dev 系統に該当データが存在しないため実害なしと判断。
3. **不透明 filename と `attributes` を同時に渡した実機確認は未実施**
   （`runs/.../e2e/SKIPPED.md` §1）。独立した引数なので相互作用は想定しにくい。
4. 既知の重複ガード（`agent and rag cannot be combined` × 2）は未整理のまま。
5. **main が `runs/**` を追跡している**（`.gitignore` に `runs/` があるのに RAGM-01/02 の
   証跡がコミットされている）。`git diff HEAD` が 10MB になり codex-review にかけられなかった
   直接原因。同期の対象外だが、main 側の衛生問題として起票を推奨。

## 同期と無関係の 1 行修正（要確認）

`packages/api/tests/test_service.py::test_api_health` が**同期前の dev で既に落ちていた**。
`jetuse_core/health.py:capability_health()` が `"agents"` を返すのに、テストの期待集合に
入っていなかった（dev 側の反映漏れ。main は health まわりを一切変えていない — 関係 5 ファイルが
merge 前後で byte 一致）。「pytest 全件パス」を満たすため期待集合に `"agents"` を足した。
**同期の差分としては異物**なので、切り離したければこの 1 行だけ落とせる。

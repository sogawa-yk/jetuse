# STATE — PREP-01（xlsx の前処理（セル位置つき抽出）と取り込み口）

- task: PREP-01
- run_id: 2026-07-30T1600_PREP-01
- branch: feat/PREP-01（base: main。共有物のため main 起点）
- area: api
- review_verdict: **PASS**（review-7。blocker 0 / major 5 / minor 1・E2E adequacy=sufficient。
  review-4 で初回 PASS → review-5 で再確認 → **人間ゲートの指示で PREP01-017（暗号化 zip の
  500 漏れ）だけを修正**し review-7 で再確認。他の指摘には手を付けていない）
- last_review_ref: runs/2026-07-30T1600_PREP-01/reviews/review-7.json
- updated_at: 2026-07-30

## やったこと

### 実装（入口だけを足す。RAGM-01 / RAGM-02 の配管は作り直していない）

- `packages/api/jetuse_core/extract_xlsx.py`（新規）: `openpyxl` を **read_only + data_only** で開き、
  シートごとに**連続する非空セルの矩形**（空行・行の飛びが区切り）を 1 チャンクにして
  `sheet`（シート名）と `cells`（A1 形式）を付ける。テキスト化は行ごとにタブ区切り。
  上限（`workbook_bytes` 10MB / `chunks` 1,000 / `chunk_chars` 2,000）は**切り詰めずに拒否**し、
  どの上限かを `limit=<名前>` として例外本文に持つ（ルート側で 422）。
  上限に収まらない塊は**行境界で分割**する（分割は切り詰めではない）。
- `rag.ALLOWED_EXTENSIONS` に `.xlsx`。`rag.prepare_upload()` が xlsx だけ
  「抽出テキスト（`<原名>.xlsx.txt`）+ **ファイル単位**の属性」に変換する。
  台帳の表示名・原本バックアップ・`sha256` は元の xlsx のまま。
- `rag_adb.chunk_units()` が xlsx を `extract_xlsx` へ振り分け（出典が行番号 → セル範囲に）。
- `rag_opensearch._extract_text()` も抽出経由に（xlsx を UTF-8 デコードして文字化け本文を入れない）。
- `POST /api/extract`（新規・要認証）: 取り込まずに `{filename, chunk_count, chunks}` を返す。
- 単体テスト: `tests/test_extract_xlsx.py`（新規 23 件）+ `tests/test_rag.py` に 20 件追加。
  `.venv/bin/pytest packages/api/tests` **538 passed** / `.venv/bin/ruff check packages/api spikes` クリーン
  （**539 passed**・PREP01-017 の修正で暗号化 zip のテストを 1 件追加。ログ: `runs/2026-07-30T1600_PREP-01/e2e/tests.log`）。

### 能力差を隠していない（ADR-0020 の決定内容）

マネージド Vector Store の属性は**ファイル単位**なので、`sheet` / `cells` は
`(ブック全体: N シート)` / `(ブック全体)` になる。1 チャンク = 1 ファイルへ割って
「セル単位で返る」ように見せる細工はしていない。導出値の利用者上書きも 422 で断る
（上書きできると能力差を偽装できるため。review-1 F-003 の対応）。

### 実環境 E2E（4 シナリオすべて PASS）

隔離: 共有 loop ADB の run 固有スキーマ `JETUSE_PREP01_<乱数>`（ADB は増やしていない）。
OCI 側は `jetuse-spike-prep01-` 接頭辞。架空データのみ。スクリプトは `spikes/prep01/`。

| # | シナリオ | 結果 | 証跡 |
|---|---|---|---|
| 0 | `POST /api/extract` が取り込まずに返す / 上限超過 422（`limit=chunks`） | PASS | `e2e/scenario-0.md` |
| 1 | `adb`: 同一ファイルの引用がチャンクごとに違う（`制約 C5:E6` / `改訂履歴 A1:C2` …） | PASS | `e2e/scenario-1.md` |
| 2 | `vector_store`: 属性はファイル単位 + **素の xlsx は 400 `unsupported_file`**（実測） | PASS | `e2e/scenario-2.md` |
| 3 | 版フィルタ `current_version='Y'` が xlsx 由来のチャンクにも効く | PASS | `e2e/scenario-3.md` |

レポート `docs/verification/PREP-01.md` / 実施しなかった範囲は `e2e/SKIPPED.md`。

## レビューで直したこと（review-1 〜 review-4）

| 指摘 | 対応 |
|---|---|
| review-1 F-003（major）: 導出した `sheet` / `cells` を利用者指定で上書きできる | 上書きは 422 で拒否（能力差を偽装できてしまうため）。単体テストで固定 |
| review-2 PREP01-001（blocker）: 塊を丸ごとメモリに溜めてから上限判定 → 上限内のファイルで OOM | 行・チャンクを**逐次処理**し、1 件作るたびに上限を見る。展開後サイズ（zip bomb）と走査セル数の上限を追加 |
| review-2 PREP01-002（major）: `select_ai` は原本を DB 側が読むので xlsx が永久に pending | 読めない形式は `error` として返す（`rag.SELECT_AI_EXTENSIONS`）。実機確認は未実施として `SKIPPED.md` に明記 |
| review-2 PREP01-003（major）: 行を読んでいる最中の破損が 500 で漏れる | 抽出の全区間を `UnsupportedWorkbook` への変換境界で囲む。破損シート XML のテストを追加 |
| review-2 PREP01-004（major）: `kind` がマネージド `spec` / ADB `doc` に割れる | 同じ値を ADB へも渡す。E2E の判定条件にも追加 |
| review-2 PREP01-005（major）: teardown が箱の照合前にファイルを消す | 照合（箱の名前・ファイル名の接頭辞）を**削除より前**に。不一致なら何も消さず中止 |
| review-3 PREP01-008（blocker）: 走査上限が非空行しか数えず、疎な巨大シートで効かない | **反復したセル**（空行込み）を数える方式へ。空行を跨ぐテストを追加 |
| review-3 PREP01-009/010/012/013（major/minor） | 左に広がる行で上限超えチャンクを作らない / `kind` をバイト長で検査（型は従来どおりスカラー可）/ 壊れた PDF を 422 に正規化 / 長いファイル名でも拡張子を残す |
| review-4 PREP01-001（major・自分の回帰） | PDF の全頁先読み（`list(reader.pages)`）を戻し、**頁を 1 つずつ**読む形に |
| review-5 PREP01-017（major・**人間ゲートの指示で修正**） | 暗号化 zip（パスワード付きブック）が `zipfile` の `RuntimeError` で 500 漏れ → 正規化対象（`_BROKEN`）に `RuntimeError` を追加。ヘッダの暗号化フラグを立てた zip の単体テストを追加（RED→GREEN）。他の指摘には手を付けていない |

## 残る指摘（PASS 下の非 blocker・人間トリアージ）

review-7 の findings（すべて非 blocker）。**PREP01-017 は人間ゲートの判断で修正済み**（上表）。
残りは公開契約・設計判断を含むため、後続トリアージとして残す（loop-protocol 手順6）。

- **kind の上限（major・review-7 PREP01-001）** `packages/api/jetuse_core/rag_metadata.py:89`: `kind` の上限を
  512 文字 → 32 バイトに狭めたのは**公開契約の変更**。ADB を使わない環境の既存クライアントも
  422 になる。狭めた理由は「両バックエンドで同じ値を入れる」ため（ADB 側が `VARCHAR2(32)`）。
  移行期間を置くか、ADB 有効時だけ検査するかは人間の判断。
- **kind の型（major・review-7 PREP01-002）** `packages/api/jetuse_core/rag.py:389`: `kind` に数値・真偽を入れると
  マネージドは値のまま・ADB は文字列（`0` / `"0"`）。さらに `rag_adb.build_where()` は
  文字列以外のフィルタ値を拒否するので、同じフィルタがバックエンドで挙動を変える。
  型を文字列に寄せるのも公開契約の変更なので判断が要る（現状は仕様書に明記）。
- **抽出口の上限（major・review-7 PREP01-003）** `packages/api/service/routes/rag.py:124`: `POST /api/extract` は
  txt/md の**チャンク数上限を分割中に見ない**ため、`rag_adb.ingest` が後段で拒否する量を
  返しうる（「投入されるものと同一」という説明とずれる）。xlsx は上限を逐次見ているので該当しない。
- **Select AI の判定根拠（major・review-7 PREP01-004）** `packages/api/jetuse_core/rag.py:510`:
  xlsx を拡張子だけで恒久的に `error` としているが、実機・一次資料での裏付けは未取得
  （`SKIPPED.md` §2 のとおり）。後続の実機確認で判定を裏付けるか、暫定表示に変えるかは判断が要る。
- **teardown の再照会（major・review-7 PREP01-005）** `spikes/prep01/teardown.py:63`:
  削除呼び出しの成否だけを記録し、Files API / Vector Store / Object Storage / ADB 行が
  実際に消えたかを再照会していない（best-effort・非同期の削除が混ざる）。
- **シート数の文言（minor・review-7 PREP01-006）** `packages/api/jetuse_core/extract_xlsx.py:138`:
  `(ブック全体: N シート)` の N は**本文があったシート数**（空シートは数えない）。
  文言を変えると既に取り込んだ属性と食い違うため据え置き、仕様・レポートに注記した。

## 未完 / 残課題

- **`select_ai` と xlsx 原本**（後続タスクとして起票が要る）: `.xlsx` 許可により原本が
  Object Storage に載るが、Select AI は DB 側パイプラインが原本を読むためアプリ側の抽出を通らない。
- `opensearch` 経路の xlsx は単体テストのみ（この環境では disabled）。
- 検証用リソースは**削除済み**（`e2e/teardown.md`。Vector Store・アップロードファイル・
  run 固有スキーマ・ローカルのウォレット/秘密。ADB は増やしていない）。

## 人間ゲート

- コミット / PR / push（未実施）。

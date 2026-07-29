# タスク: SPIKE-M1 RAG メタデータの実現方式スパイク（出典粒度・フィルタ検索）

## 目的
RAG チャンクに**任意のメタデータ**（出典位置 `file` / `version` / `sheet` / `cells` / `sha256`、
分類 `kind`、版フラグ `current_version` 等）を持たせ、
(1) 検索結果に**構造化された出典**として返す、(2) 検索時に**メタデータでフィルタ**する、
の 2 つが**どの実現方式で成立するか**を実機で確定する。

現状 `jetuse_core/chat.py` の `_extract_citations()` が返すのは `{file_id, filename, score}` のみで、
ファイル単位までしか遡れず、フィルタ検索の口も無い。この不足を埋める方式を、
**推測ではなく実機の実行結果**で決めるためのスパイクである。

本タスクは**実装ではなく検証**。JetUse の既存 RAG ルートの挙動は変えない。
方式の決定は成果物の ADR 案に対する**人間承認**をもって行う（このループでは決定しない）。

## 仕様参照
- `specs/17-demo-platform-redesign.md` §4（公開する能力 `rag.search`）
- 仕様に未記載の判断を含むため、成果物として **ADR 案**を作る（CLAUDE.md「spec-driven」）。

## 背景（なぜ優先度が高いか）
メタデータ管理は **Oracle AI Database が強みを出せる領域**である。ADB は同じ表・同じ SQL の中に
ベクタ（`VECTOR` 型）・JSON・関係列を同居させられるため、
「メタデータ絞り込みが SQL の WHERE 句」「業務表と JOIN したベクタ検索」「検索とメタ更新が同一トランザクション」
「VPD がそのまま効く」が成立する。外部ベクタストアでは原理的にできない。
したがって本スパイクは「① 外部ストアで足りるかの可否確認」ではなく、
**「① の限界を測り、② ADB 方式の優位を定量的に示す」**ことを目的とする。

裏付け: `jetuse_core/rag_select_ai.py` が作る索引表 `$VECTAB` には既に `attributes` という JSON 列がある
（`JSON_VALUE(attributes, '$.object_name')` で参照している）。

## 対象 area
api

## 前提（依存タスク / 人間の事前作業）
- 依存タスクなし。
- 実 ADB（`ops/start-adb-if-stopped.sh`）と `~/.oci/config`（`AUTH_MODE=config_file`）が使えること。
- Select AI の ADMIN セットアップ（`ops/setup-select-ai.py`）が適用済みであること。
- 検証は**まず大阪（ap-osaka-1）**で行い、Chicago 差分の確認は本タスクの対象外（後続タスク）。
  理由: JetUse 本体の提供リージョンは Chicago だが、ローカルからの検証速度を優先する。

## 検証する 3 方式
| # | 方式 | 実体 |
|---|---|---|
| ① | 外部ベクタストア | OCI Vector Store + `file_search`（現行 `jetuse_core/rag.py` の経路） |
| ② | Select AI の索引 | `DBMS_CLOUD_AI.CREATE_VECTOR_INDEX`（現行 `jetuse_core/rag_select_ai.py` の経路。`$VECTAB.attributes`） |
| ③ | ADB 自前索引 | `DBMS_VECTOR_CHAIN` 等でチャンク化・埋め込み・索引を自前に組み、メタ列を自由に持つ |

## 作業内容
- 検証用の**架空**チャンクセットを作る（10 件程度）。うち 3 件は `current_version=false`（旧版）扱い、
  `kind` は `spec` / `constraint` の 2 種を混ぜる。各件に `file` / `version` / `sheet` / `cells` / `sha256` を持たせる。
  スクリプトは `spikes/spike_m1/` 配下に置く。
- ① の実機確認: チャンク単位の属性を付与できるか／検索時にメタデータフィルタを渡せるか／
  `file_search_call.results` と message annotations に**何が返るか**（フィールドを実物のダンプで記録）。
- ② の実機確認: `CREATE_VECTOR_INDEX` の attributes に**任意のキー**を載せられるか。
  載らない場合、`$VECTAB` に列追加や JOIN で補えるか（DB 内であることの利点が実際に効くか）。
- ③ の実機確認: 最小の表（本文 + メタ列 + `VECTOR` 列）を作り、
  **「メタデータ絞り込み（WHERE）＋ ベクタ類似検索」が 1 本の SQL で書ける**ことを実証する。
- 3 方式で同一クエリを実行し、レイテンシを実測する（各 5 回以上・中央値）。
- 結果を比較表にまとめ、ADR 案を書く。

## 完了条件（検証可能な述語で）
- [ ] ①②③ それぞれについて、**実行コマンドと実際の出力**（レスポンス/SQL 結果のダンプ）が
  `docs/verification/SPIKE-M1.md` に貼られている。「ドキュメントにそう書いてある」は完了条件にしない。
- [ ] ① について、チャンク単位の属性付与の可否・フィルタ検索の可否・返却フィールド一覧が確定している。
- [ ] ② について、`attributes` に任意キーを載せられるかの可否が確定している。
- [ ] ③ について、**旧版チャンクを除外したベクタ検索が 1 本の SQL で成立する**ことを実行結果で示している。
- [ ] `docs/comparison/rag-metadata-backends.md` に 3 方式の比較表がある。比較軸は最低限
  「絞り込みの表現力 / 出典の粒度 / 実装コスト / レイテンシ実測値 / 制約・前提」。
  プリセールスへ転用できる粒度で書く（CLAUDE.md「比較ドキュメント主義」）。
- [ ] `docs/decisions/` に ADR **案**（`status: proposed`）がある。採用方式と、
  却下した方式の却下理由が書かれている。**承認は人間ゲートなので、このループで `accepted` にしない**。
- [ ] 実機で判明した Tips（制約・落とし穴）を `docs/tips.md` に追記している。
- [ ] `.venv/bin/pytest packages/api/tests` 全緑・`.venv/bin/ruff check packages/api` クリーン
  （既存の回帰が無いこと。本タスクは既存コードを変更しない想定だが、変更した場合も緑を維持）。
- [ ] STATE.md の `review_verdict` が PASS。

## E2E シナリオ（実環境 / jetuse-dev・複数）
本タスクは検証そのものが実環境実行なので、以下を E2E 証跡として `runs/<run-id>/e2e/` に記録する。

- [ ] シナリオ1（③ の版フィルタ）: 実 ADB に架空チャンク 10 件を投入 →
  `WHERE current_version = 'Y'` 付きのベクタ検索を実行 → **旧版 3 件が 1 件も返らない**ことを結果で示す。
  フィルタ無しでは旧版が返ることも併せて示す（フィルタが効いていることの対照）。
- [ ] シナリオ2（③ の出典粒度）: 同じ検索で、各ヒットに `file` / `version` / `sheet` / `cells` が
  **構造化された値として**返ることを示す（本文への埋め込みではなく列/JSON として）。
- [ ] シナリオ3（① の限界測定）: 実 OCI Vector Store に同じ架空チャンクを投入し、
  属性付与とフィルタ検索を試みる。できない場合は**エラー内容そのもの**を証跡として残す
  （「できなかった」という記述だけにしない）。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記（無言スキップ禁止）。

## 成果物
- `docs/verification/SPIKE-M1.md`（実行ログ付きレポート）
- `docs/comparison/rag-metadata-backends.md`（3 方式の比較・プリセールス転用可）
- `docs/decisions/ADR-00xx-rag-metadata-backend.md`（**案**。番号は既存の続き）
- `docs/tips.md` への追記
- `spikes/spike_m1/`（検証スクリプト。架空データ生成・3 方式の実行・レイテンシ計測）

## 非ゴール / 禁止事項
- **JetUse 本体の実装を変更しない**。`rag.py` / `rag_select_ai.py` / `chat.py` の挙動・API 契約を変えない。
  実装は本スパイクの ADR が承認された後の別タスク。
- **顧客データを持ち込まない**。顧客案件の Excel 原本・`spec_chunks.json`・実際の API 仕様値を
  この リポジトリへコピーしない。検証はすべて**架空のダミーチャンク**で行う。
- 前処理（xlsx 抽出・OCR の RAG 結線）は本タスクの対象外（後続タスク）。
- Chicago リージョンでの再確認は対象外（後続タスク）。
- 検証用に作る OCI リソースは **`jetuse-spike-` プレフィックス必須**。作ったら片付ける。
- 認証情報・テナンシ/コンパートメント OCID・エンドポイント実値をコミットしない。
- 未承認のコミット / PR / push を行わない。ADR を `accepted` にしない（人間ゲート）。

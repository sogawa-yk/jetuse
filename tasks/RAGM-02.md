# タスク: RAGM-02 Oracle AI Database バックエンド（`adb`）の追加

## 目的
高機能側の RAG バックエンドとして、Oracle AI Database の自前索引を `rag_backend='adb'` で
選べるようにする。**チャンク単位の出典**と**SQL の全表現力での絞り込み**を提供する。

## 仕様参照
- `docs/decisions/ADR-0020-rag-metadata-backend.md` §2（Accepted）
- 実機で成立済みの形: `docs/verification/SPIKE-M1.md` ③-a〜③-d
  （表定義・DB 内埋め込み・版フィルタ 1 本 SQL・構造化出典の実行結果）

## 対象 area
api

## 前提（依存タスク / 人間の事前作業）
- ADB 23ai 以上（`VECTOR` 型・`DBMS_VECTOR_CHAIN`）。
- 資格証明は **`DBMS_VECTOR_CHAIN.CREATE_CREDENTIAL`** で作る（`DBMS_CLOUD` のものは引けない。
  SPIKE-M1 で `ORA-20003` を実測）。
- **先にスケール検証を済ませること**（下記「前提の検証」）。

## 前提の検証（本実装の前に必ず）
SPIKE-M1 は 10 チャンクでしか測っていない。10 行ではオプティマイザがベクタ索引を使わず
全件走査を選ぶ（実行計画で確認済み）。よって本実装前に:
- [ ] 数万〜数十万チャンク規模で「メタデータ WHERE + HNSW/IVF」の実行計画と再現率を測る。
- [ ] `TARGET ACCURACY` に対する実再現率を記録する。
結果次第で索引種別・チャンク設計を変える。

## 作業内容
- 新規モジュール（既存 RAG 経路は据え置き）。表は本文 + メタ列 + `JSON` 列 + `VECTOR` 列。
- 取り込み: チャンク化 → 埋め込み → 投入。DB 内埋め込み（`UTL_TO_EMBEDDING`）と
  クライアント側埋め込みのどちらを既定にするか決めて実装する。
- 検索: 「メタデータ絞り込み + ベクタ類似検索」を 1 本の SQL で。出典は列 / JSON で返す。
- `rag_backend='adb'` を API とバックエンド状態（`backends`）に追加する。
- 既存の 3 バックエンドの取り込み状況バッジと同じ枠組みに乗せる。

## 完了条件（検証可能な述語で）
- [ ] `rag_backend='adb'` で取り込み → 検索 → 回答までが実環境で通る。
- [ ] citations にチャンク単位の `sheet` / `cells` が載る（同一ファイル内で**値が異なる**ことを示す。
      これがマネージド Vector Store との決定的な差）。
- [ ] `current_version='Y'` 相当の絞り込みで旧版が 0 件、フィルタ無しでは返ることを示す。
- [ ] スケール検証の結果が `docs/verification/RAGM-02.md` にある（実行計画付き）。
- [ ] `.venv/bin/pytest packages/api/tests` 全緑・`.venv/bin/ruff check packages/api` クリーン。

## E2E シナリオ（実環境 / jetuse-dev）
- [ ] シナリオ1: 同一ファイル由来の複数チャンクが**別々の** `cells` を返すこと。
- [ ] シナリオ2: 業務表と JOIN したベクタ検索が 1 クエリで成立すること（サンプル表で可）。
- [ ] シナリオ3: 版フィルタの対照（有り 0 件 / 無し ヒット）。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記。

## 成果物
- 新規モジュール + テスト / `docs/verification/RAGM-02.md`（スケール検証を含む）

## 非ゴール / 禁止事項
- 既存 3 バックエンドの挙動変更。
- VPD の実証（別タスク。未実証のまま「できる」と資料に書かない）。
- 顧客データの持ち込み。認証情報・OCID のコミット。

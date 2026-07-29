# タスク: RAGM-03 バックエンドの能力差を API とフロントから見えるようにする

## 目的
バックエンドを選べるようにしても、**選ぶと何が増えるのかが伝わらなければ選べる意味がない**。
「Oracle AI Database を選ぶとここまでできる」を、API と画面の双方で示す。

## 仕様参照
- `docs/decisions/ADR-0020-rag-metadata-backend.md` §3（Accepted。2026-07-29 のユーザー指示が根拠）
- 能力カタログの追加手順: `packages/api/jetuse_core/capabilities.py` の冒頭コメント（正本）
- 比較表の中身: `docs/comparison/rag-metadata-backends.md`

## 対象 area
api, web

## 前提（依存タスク / 人間の事前作業）
- RAGM-01（属性付き `vector_store`）と RAGM-02（`adb`）が入っていること。
  能力差の表示だけ先に入れると、画面に「使える」と出ているのに動かない状態になる。

## 作業内容
- `capabilities.py` の `rag.search` ディスクリプタに **バックエンドごとの能力**を機械可読で追加する。
  軸は比較ドキュメントと揃える: 出典粒度 / 絞り込みの表現力 / 業務データ結合 /
  行レベル制御 / メタ更新の整合性。
  - **未実証のものを「できる」と書かない**（例: VPD は SPIKE-M1 で未実証）。
    実証済みかどうかを区別できる形にする。
- `GET /api/capabilities` の既存構造を壊さずに載せる（`tests/test_capabilities.py` の
  ルート乖離検出が通ること）。
- **フロント**: `packages/web/src/pages/rag.tsx` の既存バックエンド選択 UI に、
  選択中のバックエンドで「使える機能 / 使えない機能」を出す。
  - 既存の取り込み状況バッジ（indexed / pending / disabled）とは**別物**として設計する。
    あちらは「取り込めたか」、こちらは「何ができるか」。
  - `adb` を選ぶと版フィルタとセル範囲出典が有効になることが画面で分かること。
  - 文言は i18n に載せる（既存の `rag.backend.*` に倣う）。

## 完了条件（検証可能な述語で）
- [ ] `GET /api/capabilities` の `rag.search` にバックエンド別の能力が載り、
      未実証項目が実証済み項目と区別できる。
- [ ] `packages/api/tests` のカタログ整合テストが緑。
- [ ] 画面でバックエンドを切り替えると、使える機能の表示が変わる。
- [ ] `npm --prefix packages/web run test` / `run lint` / `run build` が緑。
- [ ] `.venv/bin/pytest packages/api/tests` 全緑・`.venv/bin/ruff check packages/api` クリーン。

## E2E シナリオ（実環境 / jetuse-dev）
- [ ] シナリオ1: 配備した URL で `/api/capabilities` を取得し、バックエンド別能力が返ることを示す。
- [ ] シナリオ2: 画面でバックエンドを切り替え、能力表示が切り替わることをスクリーンショットで示す。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記。

## 成果物
- `capabilities.py` / `rag.tsx` / i18n の変更、テスト
- `docs/verification/RAGM-03.md`（画面のスクリーンショットを含む）

## 非ゴール / 禁止事項
- バックエンド本体の実装（RAGM-01 / RAGM-02）。
- 未実証の能力を「対応」と表示すること。

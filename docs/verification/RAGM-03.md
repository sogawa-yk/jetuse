# RAGM-03 検証レポート: バックエンドの能力差を API と画面から見えるようにする

実施日: 2026-07-30 / リージョン: ap-osaka-1 / 仕様の正本: `docs/decisions/ADR-0020-rag-metadata-backend.md` §3
比較表の中身: `docs/comparison/rag-metadata-backends.md`
証跡: `runs/2026-07-30T1800_RAGM-03/e2e/`（実施しなかった範囲は同ディレクトリの `SKIPPED.md`）

## 結論（先に）

**「Oracle AI Database を選ぶと何が増えるか」が、API（`GET /api/capabilities`）と画面の双方で
そのまま読める状態になった。** そして **未実証のものは「未実証」と出る**。

1. `rag.search` ディスクリプタに `backend_capabilities` を追加した。4 バックエンド × 5 軸
   （出典粒度 / 絞り込みの表現力 / 業務データ結合 / 行レベル制御 / メタ更新の整合性）を
   機械可読で持ち、各項目に `support` / `verified` / `detail` / `evidence` が付く。
2. 画面（`rag.tsx`）のバックエンド選択に `adb` を足し、選択中のバックエンドで
   「使える / 条件付き / 使えない / **未実証**」を出す。ADB を選ぶと
   **セル範囲つき出典**と**版フィルタ（既定で現行版のみ）**が「使える」として現れる。
3. **VPD（行レベル制御）と業務表 JOIN は「未実証」**。SPIKE-M1 でどちらも実行結果を残していない
   （`runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md` 3）ため、「できる」とは書いていない。

## 何を機械可読にしたか

```
rag.search.backend_capabilities
├── axes[5]            : 比較ドキュメントと同じ 5 軸
├── support_levels     : yes / limited / no / unverified の意味
└── backends{4}        : vector_store / adb / select_ai / opensearch
    └── axes{5} → { support, verified, detail, evidence }
```

`verified` は手で立てず `support` から導出する（`_axis()`）。`unverified` のときだけ `false`。
つまり **「未実証なのに verified=true」という状態を構造的に作れない**。

| backend | 出典粒度 | 絞り込み | 業務データ結合 | 行レベル制御 | メタ更新整合 |
|---|---|---|---|---|---|
| vector_store | limited（ファイル単位） | limited（属性 eq/and/or/gte・明示指定） | no | no | limited（結果整合） |
| **adb** | **yes**（チャンク単位・セル範囲） | **limited**（常に現行版のみ。API から条件を渡す口は無い） | **unverified** | **unverified** | **yes**（同一Tx） |
| select_ai | limited（ファイル + offset） | no（ORA-20048） | no | unverified | no（refresh で欠損） |
| opensearch | unverified ×5（ADR-0020 の比較対象外） | | | | |

数字と制約の出所はすべて `evidence` に入れてある（SPIKE-M1 / RAGM-01 / RAGM-02 / PREP-01 /
比較ドキュメント）。画面ではバッジの `title` としてそのまま読める。

## 画面（取り込み状況バッジとは別物にした）

| | 取り込み状況バッジ（既存） | 能力パネル（本タスク） |
|---|---|---|
| 問い | そのファイルを**取り込めたか** | そのバックエンドで**何ができるか** |
| 出る場所 | ファイル一覧の各行 | チャット欄の上（選択中バックエンド 1 つ分） |
| 値 | indexed / pending / error / disabled | yes / limited / no / **unverified** |

既存バッジの意味は変えていない。`adb` は API が前から返していた（RAGM-02）ので、
表示対象に加えただけである。文言は i18n（`rag.cap.*` / `rag.backend.adb` / `rag.be.adb`）に置いた。

証跡のスクリーンショット（同じ画面・選択だけが違う）:

- `docs/verification/e2e-screenshots/RAGM-03-vector_store.png`
- `docs/verification/e2e-screenshots/RAGM-03-adb.png` — 「✓ 使える 出典の粒度 — チャンク単位。
  xlsx はシート名とセル範囲まで返る(実測例: 『制約』C5:E6 / 『改訂履歴』A1:C2)」
- `docs/verification/e2e-screenshots/RAGM-03-select_ai.png`（DOM スナップショットは e2e/ に同梱）

## 実装（何を足したか）

| 変更 | 内容 |
|---|---|
| `packages/api/jetuse_core/capabilities.py` | `RAG_BACKEND_CAPABILITIES` と `_axis()`。`rag.search` に `backend_capabilities` を追加（既存キーは不変） |
| `packages/api/tests/test_capabilities.py` | 選べるバックエンド（`ChatRequest.rag_backend`）との一致 / 全軸の充足 / **未実証を「できる」と書かせない**不変条件 / 未実証 3 項目の期待値固定 |
| `packages/web/src/pages/rag/capabilityCatalog.ts` | バックエンドの語彙と能力カタログからの取り出し（能力の事実は画面に持たない） |
| `packages/web/src/pages/rag/BackendCapabilities.tsx` | 能力パネル（API の記述を描くだけ） |
| `packages/web/src/pages/rag/BackendStatusBadges.tsx` | 既存の取り込み状況バッジを切り出し、`adb` を表示対象に追加（意味は不変） |
| `packages/web/src/pages/rag.tsx` | `adb` を選択肢に追加・能力パネルの配置・カタログ取得・アップロードの受入形式 |
| `packages/web/src/pages/rag/uploadFormats.ts` | 画面の受入形式（API の `ALLOWED_EXTENSIONS` と揃える。**xlsx を追加** = RAGM03-002 の是正） |
| `packages/web/src/i18n/dict.{ja,en}.ts` | `rag.cap.*` / `rag.backend.adb` / `rag.be.adb`・凡例更新 |

## 分かったこと・注意

- **`adb` の絞り込みは `limited` にした**（人間ゲートでの是正 RAGM03-001）。DB 側は SQL の WHERE で
  version / file / sheet / kind まで絞れる実装があるが、**外から使える形はまだ「常に現行版のみ」まで**で、
  チャット API から条件を渡す口は無い（`rag_filters` は vector_store 専用・adb 指定時は 400）。
  detail に但し書きがあるからと `yes` にはしない — 表の 1 行だけを見た人が誤解する形を避ける。
  口を開けるかは別タスクの判断。
- **業務表 JOIN も未実証だった**。ADR / 比較ドキュメントは「可」と書いているが、SPIKE-M1 は
  業務表を作らず実行結果を残していない。「SQL だから書ける」は実証ではないので `unverified` にした。
- `select_ai` の xlsx の扱いは PREP-02 が実機確認中のため「未確認」と注記した（断定しない）。
- `opensearch` はメタデータ観点で未評価。5 軸すべて `unverified` にしてある。

## 残課題

- VPD / Data Redaction がベクタ検索に効くか、業務表 JOIN の実行結果 — どちらも実機確認が要る。
  確認できたら `capabilities.py` の該当軸を `unverified` から上げる（テストの `UNPROVEN_AXES` も更新）。
- `opensearch` の 5 軸の評価。
- 配備済み URL（API Gateway 経由）での確認は未実施（`SKIPPED.md` 1。作業機にコンテナランタイムが無い）。
- 画面の「?」ヘルプと RAG 構成図が 3 バックエンドのままで ADB を含まない（residual・後続）。
- 能力カタログ応答が部分的に壊れている場合、描画時に例外になりうる（外形検証のみ。residual・後続）。
- `adb` の絞り込み条件をチャット API から渡せるようにするか（RAGM03-001 の残り。仕様判断が要る）。

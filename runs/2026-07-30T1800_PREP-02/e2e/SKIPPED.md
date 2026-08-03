# 実施しなかった範囲と理由（PREP-02）

無言のスキップはしない。以下は**意図的に実施していない**、または**実施できなかった**範囲。

## 1. 配備済みインスタンスへの実ネットワーク E2E（LB / 認証 / Resource Principal）

実施したのは**ローカルプロセスから実 OCI を叩く**形（実 ADB・実 Object Storage・実 GenAI）。
公開 LB / API Gateway / Identity Domain 認証 / `AUTH_MODE=resource_principal` は通していない。
本タスクの変更は `packages/api/jetuse_core/rag.py` のバッジ判定 1 箇所に閉じており、
配備形態に依存する要素を触っていない（それらは `docs/verification/PUBLIC-DEPLOY-E2E.md` 側の担保）。

## 2. `refresh_rate` による自動同期の実測（**未確認**）

観測したのは `CREATE_VECTOR_INDEX` **時点**の取り込みだけ。既存の索引にあとから xlsx を
置いたときに、`refresh_rate`（60 分）の自動同期が xlsx を拾うかは**確認していない**
（1 回の観測に 1 時間以上かかり、本タスクの問い＝「xlsx を扱えるのか」には答えが出ているため）。
バッジの `pending` → `indexed` の遷移時間はこの同期に依存する。

## 3. xlsx の「難しい」ケース（**未確認**）

架空ブックは 2 シート・数行・5.6KB の小さなものである。次は観測していない:

- 大きなブック（数万行）でチャンクがどう割れるか、途中で切れないか
- 数式・グラフ・ピボット・画像・パスワード保護されたブック
- `.xlsb` / `.xls`（旧形式）。**そもそもアプリが受け付けない**（`ALLOWED_EXTENSIONS` は
  pdf / txt / md / xlsx）ので本タスクの対象外
- 壊れたブックを置いたときに索引全体が失敗しないか

「xlsx は Select AI で扱える」は、**上記の範囲（小さな通常のブック）で実測した**という意味である。

## 4. `opensearch` バックエンドの xlsx（未確認のまま）

この環境では無効（`backends.opensearch = disabled`）。PREP-01 から状況は変わっていない。

## 5. 能力差の UI 表示

非ゴール（RAGM-03 の担当）。`packages/web` と `capabilities.py` は本タスクで触っていない。

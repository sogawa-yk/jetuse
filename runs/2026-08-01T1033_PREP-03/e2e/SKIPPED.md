# 実施しなかった範囲と理由（PREP-03）

無言のスキップはしない。以下は**意図的に実施していない**範囲。

## 1. 配備済みインスタンスへの実ネットワーク E2E（LB / 認証 / Resource Principal）

実施したのは **プロセス内 ASGI**（`fastapi.testclient.TestClient`）からの実 API 呼び出しで、
相手側（OCI Document Understanding・OCI Generative AI の Files / Vector Stores / 埋め込み /
生成・ADB）はすべて実物。公開 LB・API Gateway・Identity Domain 認証・
`AUTH_MODE=resource_principal` は通っていない。

理由: 本タスクの変更は API パッケージ内（OCR の結線・取り込み口・`POST /api/extract`）と
画面の対応形式一覧に閉じており、配備形態に依存する要素（LB / 認証 / 署名方式）を触っていない。
それらの経路は `docs/verification/PUBLIC-DEPLOY-E2E.md` で別途担保されている。
`loop-config.yml` の api area の `deploy_cmd` は「実 ADB へのマイグレーション適用」であり、
それは実施済み（`deploy.log`）。

**ただし OCR だけは配備先の認可が別**である。配備先（Container Instance / Functions）は
Resource Principal で動くため、動的グループに `use ai-service-document-family` が要る
（`docunderstand.py` 冒頭。未付与だと 404 → このタスクでは 503 として表面化する）。
ローカルの `config_file` 認証では通ったが、**配備先で通ることは確認していない**。
IAM は本タスクの禁止事項なので変更していない。

したがってこのタスクの完了主張の範囲は「**コードと取り込み経路は実 OCI（Document Understanding /
Generative AI / ADB）に対して検証済み。配備先（Resource Principal）での OCR 認可は人間ゲート待ちで
未検証**」である。配備先で動く証拠ではない。人間が IAM を承認・適用したあと、配備済み API に対して
少なくともスキャン PDF と画像の 2 シナリオを再実施する必要がある。

## 1-b. 共有 loop ADB を起動したこと（**2026-08-01 事後承認済み**）

E2E の前提として `jetuse-loop-adb` が STOPPED だったので **start** した（データ・構成は不変）。
**2026-08-01 に人間ゲートで事後承認された。ADB は起動したまま運用する判断**（停止不要）。

承認の根拠は、この起動が例外操作ではなく**通常の開発フローの一部**として文書化されていること:
`loop-config.yml:125` の api area の `deploy_cmd` が
`ops/start-adb-if-stopped.sh && .venv/bin/python -m jetuse_core.migrate` であり、
実環境 E2E の deploy 手順に停止中 ADB の起動が最初から含まれている。
同じ手順は `tasks/RP-01.md:21` / `tasks/SPIKE-M1.md:36` の前提にも、
`docs/guides/demo-scenarios.md:83` / `docs/guides/onboarding.md:49` にも書かれている
（ADB は夜間停止運用）。詳細は `deploy.log` 末尾。

## 2. `select_ai` バックエンドでのスキャン PDF・画像（**未対応のまま**）

Select AI は Object Storage 上の**原本**を DB 側（`DBMS_CLOUD_AI` / Oracle Text）が読む方式で、
アプリ側の OCR を通らない。テキスト層の無い PDF・画像はテキスト化できないため、
**索引に入らない = 取り込み状況バッジが `pending` のまま**になる（E2E の
`backends` にそのまま出ている: `'select_ai': 'pending'`）。

このタスクでは**扱いを変えていない**。PREP-01 が xlsx を拡張子だけで恒久 `error` に
したのを PREP-02 の実測が撤回した経緯があり、同じことを実測なしに繰り返さないため
（Select AI のプロファイル / 索引の作成には ADMIN セットアップと数十分が要る）。
→ 後続タスクとして起票が要る（`docs/verification/PREP-03.md` の残課題）。

## 3. `opensearch` バックエンド

この環境では無効（E2E の `backends` が `disabled`）。画像を UTF-8 デコードして
文字化け本文を "indexed" にしないよう `rag_opensearch._extract_text` を OCR 経由へ直したが、
**実機では確認していない**（単体テストのみ: `test_opensearch_ocrs_images_instead_of_indexing_mojibake`）。

## 4. 5 ページを超えるスキャン PDF の分割 OCR

`docunderstand` の既存機能（ENH-07b。5 ページ以下に分割して並列 OCR）であり、
このタスクは**結線しかしていない**。実測は SPIKE-E4 / ENH-07b 済みなので再確認しない
（タスクの禁止事項「実測済みの制約を再確認しない」）。
本タスクの E2E は 2 ページ（1 回の同期呼び出しに収まる）で行った。

## 5. 表構造の復元・レイアウト解析 / Word・PowerPoint

いずれも本タスクの非ゴール。実装していない（OCR は `tables=False` で呼んでいる）。

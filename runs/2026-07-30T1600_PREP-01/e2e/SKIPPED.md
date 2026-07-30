# 実施しなかった範囲と理由（PREP-01）

無言のスキップはしない。以下は**意図的に実施していない**範囲。

## 1. 配備済みインスタンスへの実ネットワーク E2E（LB / 認証 / Resource Principal）

実施したのは **プロセス内 ASGI**（`fastapi.testclient.TestClient`）からの実 API 呼び出しで、
相手側（OCI Generative AI の Files / Vector Stores / 埋め込み / 生成、および ADB）はすべて実物。
公開 LB・API Gateway・Identity Domain 認証・`AUTH_MODE=resource_principal` は通っていない。

理由: 本タスクの変更は API パッケージ内（xlsx 抽出・取り込み口・`POST /api/extract`）に閉じており、
配備形態に依存する要素（LB / 認証 / 署名方式）を触っていない。それらの経路は
`docs/verification/PUBLIC-DEPLOY-E2E.md` で別途担保されている。
`loop-config.yml` の api area の `deploy_cmd` は「実 ADB へのマイグレーション適用」であり、
それは実施済み（`upload.md` の冒頭・`migrate` 出力）。

## 2. `select_ai` バックエンドでの xlsx（**扱いは決めた / 実機検証は未実施**）

Select AI は Object Storage 上の**原本**を DB 側（`DBMS_CLOUD_AI` のパイプライン）が取り込む方式で、
本タスクが足した抽出（アプリ側）を通らない。`.xlsx` を許可したことで原本の xlsx がバケットに載る。

そのため、**取り込み状況バッジで xlsx を `error` として返す**ようにした
（`rag.SELECT_AI_EXTENSIONS` = pdf / txt / md。`pending` のままにすると「いつか索引に入る」という
嘘の期待を作り、永久に入らないことに誰も気づけない）。単体テストで固定済み。

実機での確認（Select AI の索引が xlsx をどう扱うか、抽出テキストを別オブジェクトとして
投入すべきか）は未実施。この環境では Select AI のプロファイル / 索引が未作成で、
作成には数十分と ADMIN セットアップ（`ops/setup-select-ai.py`）が要るため、本タスクでは行わない。
→ 後続タスクとして起票が要る（`docs/verification/PREP-01.md` の「残課題」）。

## 3. `opensearch` バックエンドでの xlsx

この環境では無効（`backends.opensearch = disabled`。`upload.md` 参照）。
xlsx をそのまま UTF-8 デコードして文字化け本文を投入しないよう
`rag_opensearch._extract_text` を抽出経由に直したが、**実機では確認していない**（単体テストのみ）。

## 4. 能力差の UI 表示 / OCR / docx / pptx

いずれも本タスクの非ゴール（UI は RAGM-03、他形式は別タスク）。実装もしていない。

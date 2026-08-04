# RAG-03 検証レポート: Select AI RAGバックエンド切替

日付: 2026-06-11
仕様: specs/09-rag.md [RAG-03]
状態: **実機E2E完了**（jetuse-dev-adb 26ai化 + イメージ0.10.1）

## 前提作業（同日）

- **jetuse-dev-adbを19c→26aiへアップグレード**（ユーザー承認）。OCIの19cからのアップグレード先は26ai（23ai機能包含）。同期更新不可・スケジュールAPI必須（`--db-version 26ai --is-schedule-db-version-upgrade-to-earliest true` → 約30分後に自動実行、所要約12分）。アップグレード後のアプリ動作（会話CRUD・チャット）に影響なし
- 検証用ADB削除（ユーザー指示）: jetuse-spike-adb / jetuse-spike-adb23。`.env` の `ADB_OCID` をjetuse-dev-adbに修正

## 実装

- `/api/chat/stream` の `rag_backend: vector_store | select_ai`。select_aiは `DBMS_CLOUD_AI.GENERATE(action=>'narrate')` をスレッド実行し単発delta+keepaliveで返す
- **per-user分離**: `JETUSE_RAG_{sha1(owner)[:8]}` のprofile+vector indexを遅延作成。索引の取り込み元は**RAG-01の原本バックアップ `rag/{owner}/`**（同じアップロードが両バックエンドに供給される）
- 応答末尾の `Sources:` をcitationsイベントに変換（`{uuid}_` プレフィックスを表示名から除去）
- ADMINセットアップ `ops/setup-select-ai.py`: JETUSE_APPへ `EXECUTE ON DBMS_CLOUD / DBMS_CLOUD_AI / DBMS_CLOUD_PIPELINE` + GenAI/OSホストACL + APIキーcredential（Vault化はPhase 8）
- UI: /ragにバックエンドセレクタ + select_ai注記（初回数分・同期60分間隔・会話文脈なし）

## 実機E2E（API GW経由、同一アップロード文書）

| ケース | 結果 |
|---|---|
| vector_storeバックエンド | 正答+引用（従来どおり） |
| select_ai 初回（profile+索引構築込み） | **28.5s**で正答+引用（keepaliveで切断なし） |
| select_ai 2回目以降 | **2.6s**で正答（SPIKE-08のDB直接1.6s+アプリ経路） |
| 引用表示 | Sources→citationsイベント変換、表示名正常 |

- pytest 38件 / ruff / web lint+build クリーン

## ハマりどころ（tips.mdにも記録）

- `ORA-20000: Missing EXECUTE privilege on DBMS_CLOUD_PIPELINE` — ベクトル索引の同期はパイプライン実装のため、DBMS_CLOUD/DBMS_CLOUD_AIに加えて**DBMS_CLOUD_PIPELINEのEXECUTE**が必要
- 失敗時にprofileだけ残ると以後の遅延作成がスキップされる → クリーンアップ手順は本レポートのスクリプト参照

## 制約（仕様どおり）

- select_aiは会話文脈なし（単発質問）、モデル固定（llama）、アップロード反映は索引refresh_rate（60分）間隔

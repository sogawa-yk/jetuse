# 実施しなかった範囲と理由（無言スキップをしない）

## 1. 配備済み dev アプリスタック（Container Instance / API Gateway）への再配備

**実施していない。** 代わりに FastAPI アプリ（`service.main`）を **実 ADB・実 OCI Generative AI に
接続したまま** in-process（TestClient）で起動し、`POST /api/rag/files` → `POST /api/chat/stream` の
アプリ全経路を実 API に対して通した（`scenario-1.md` / `scenario-2.md` / `guard.md`）。

理由: RAGM-01 の変更は API 層（`jetuse_core/rag.py` / `chat.py` / `rag_metadata.py` / ルート）に閉じており、
Container Instance へ配備しても通る経路は同じ。共有 dev スタックへの `terraform apply` は他タスク
（RAGM-02 が並行実行中）の実行環境を差し替えるため、この変更のためだけには行わない。
HTTP 境界（SSE のフレーミング・API Gateway の readTimeout）はこの変更で触っていない。

## 2. 属性キー 16 個超過の実 API 拒否

**実施していない（構造的に到達不能）。** 許可キーは 8 個（`rag_metadata.ATTRIBUTE_KEYS`）で、
それ以外は 422 で弾くため、16 個超の属性が OCI へ届く経路が無い。上限そのものの実測は
SPIKE-M1 ①-d 済み。アプリ側の防御は単体テスト（`test_allowed_keys_fit_provider_limit`）で固定した。

## 3. `in` フィルタの上流 400 の再実測

**実施していない。** アプリが 422 で手前で弾くため、上流呼び出しに到達しない（`guard.md` の 2 行目）。
上流が 400 を返すこと自体は SPIKE-M1 ①-b で実測済み。

## 4. 取り込み後の属性更新（`vector_stores.files.update`）

**本タスクの範囲外。** 版の付け替え（`current_version` Y→N）を可能にする API は SPIKE-M1 ①-e で
実測済みだが、RAGM-01 の受け入れ条件は「取り込み時に属性を付ける」「版で絞る」までであり、
版の切り替え運用（どの操作で誰が付け替えるか）は仕様が無い。仕様化はループの外の判断。

## 5. フロントエンド（能力差の表示・引用 UI）

**非ゴール（RAGM-03）。** `packages/web` は変更していない。API 契約は追加のみで後方互換
（`{file_id, filename, score}` を残す）なので、既存フロントは無変更で動く。

## 6. `adb` バックエンド

**非ゴール（RAGM-02）。** 本タスクは既定の `vector_store` バックエンドのみを対象とした。

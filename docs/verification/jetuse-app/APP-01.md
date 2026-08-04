# APP-01 検証レポート: FastAPIスケルトン

日付: 2026-06-10
仕様: specs/03-app-skeleton.md
状態: **完了（ローカル + API GW経由のSSE実機疎通まで確認）**

## API GW経由の実機疎通（同日追記）

- コンテナ化（`packages/api/Containerfile`）→ OCIR push（`jetuse-dev-api:0.1.1`）→ Container Instance起動 → **API GW `https://{gw}/api/chat/ping` でSSE成立**（keepalive→dataイベント3件→[DONE]、HTTP 200 text/event-stream）
- ハマり2件（実機確定）:
  1. **private OCIRはCIにimage pull secretが必須**（無いと `image could not be pulled` でCI作成失敗）。BASIC型（username/passwordをbase64）をTerraformモジュールに追加して解決
  2. **`pip install .` ではuvicornが入らない**（pyprojectのdev extras）→ CMDのuvicorn不在で**exit 128 / CONTAINER_TERMINATED**（lifecycle-stateはACTIVEのままなので注意）。Containerfileで `pip install . uvicorn` に修正。以後はpodmanローカル起動のスモークテストをpush前に必須化

## 実行結果

| チェック | 結果 |
|---|---|
| `ruff check .` | All checks passed |
| `pytest`（4件: healthz / SSE+keepalive / 認証必須時401 / OIDC未設定fail-closed 500） | 4 passed |
| uvicorn実起動 + curl | `/healthz` → `{"status":"ok"}`、`/api/chat/ping` → `: keepalive` → dataイベント → `data: [DONE]`（SSE成立） |

## 実装内容（ADR-0005の3点構成）

- `jetuse_core/`（共有パッケージ）: settings（pydantic-settings, feature flags）/ logging（JSON Lines）/ auth（PyJWT+JWKS。`AUTH_REQUIRED=false` 既定、OIDC未設定でトークン提示時はfail-closed）/ genai（`spikes/common.py` 昇格。DP/CP 2ホスト・`OpenAi-Project`/`CompartmentId` ヘッダ対応）
- `service/`（CI用FastAPI）: app factory、アクセスログmiddleware、`/healthz`、`/api/chat/ping`（SSEデモ。keepaliveコメント送出 = ADR-0003要件）
- `fn/`（Functionsハンドラ置き場）: README（運用ルール: jetuse_core共用、6MB上限対策）

## 残課題（後続タスクで実施）

- [ ] コンテナ化（Containerfile）→ OCIR push → CI起動 → API GW経由疎通（INFRA-01 apply後）
- [ ] CI/Functions上でのリソースプリンシパル署名切替（`jetuse_core/genai.py` のTODO）
- [ ] 実JWT検証（INFRA-02でissuer/JWKS確定後）

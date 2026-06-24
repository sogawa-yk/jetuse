# specs/03 — Phase 1 アプリ基盤（APP-01: FastAPIスケルトン）

状態: ドラフト（2026-06-10作成）
仕様参照: specs/00-architecture.md / ADR-0003（SSE・keepalive）/ ADR-0005（Functions+CI、共有パッケージ）

## [APP-01] FastAPIスケルトン

### 目的

ADR-0005の3点構成（共有パッケージ / CI用FastAPI / Functionsハンドラ）の骨格を作り、SPIKE-01の2系統クライアント（Responses / Chat Completions）の生成ロジックを `spikes/common.py` から昇格する。

### 前提

- INFRA-01のapply前でもローカルで開発・テスト可能であること（API GW経由の疎通確認はapply後）
- INFRA-02（OIDC）未完のため、JWT検証は**実装するが feature flag で無効化可能**にする（既定: dev無効）

### 構成（packages/api/）

```
packages/api/
  pyproject.toml        # パッケージ定義(jetuse-api)。dev依存: pytest, ruff
  jetuse_core/            # 共有パッケージ(CI/Functions両方から使う — ADR-0005)
    settings.py         #   pydantic-settings。環境変数/.envから。feature flags含む
    logging.py          #   構造化ログ(JSON Lines)
    auth.py             #   OIDC JWT検証(PyJWT+JWKS)。AUTH_REQUIRED=false でバイパス
    genai.py            #   OpenAI互換クライアント生成(spikes/common.py昇格。2ホスト・Projectヘッダ対応)
  service/              # CI用FastAPI(SSE系 — ADR-0003)
    main.py             #   app factory、/healthz、/api/chat/ping(SSEデモ+keepalive)
  fn/                   # Functionsハンドラ置き場(個々のfnはAPP-02以降で追加)
    README.md
  tests/
```

### 要件

1. 設定は `jetuse_core.settings.Settings`（環境変数 > .env）。秘密はコードに置かない
2. ログはJSON 1行/イベント（timestamp, level, message, extra）。uvicornアクセスログと併存
3. JWT検証: `Authorization: Bearer` をJWKS（IDCS）で検証するFastAPI dependency。`AUTH_REQUIRED=false`（既定）で署名検証をスキップせず**認証自体を不要化**（INFRA-02完了までの暫定）
4. SSE: `/api/chat/ping` がSSEで5イベント+keepaliveコメントを送出（ADR-0003の実装要件の最小実証。API GW apply後の疎通確認にも使う）
5. `jetuse_core.genai`: 推論用(DP)/Vector Store CRUD用(CP)クライアントを設定から生成。`OpenAi-Project`・`CompartmentId` ヘッダはspecs/00の未文書仕様に従う
6. テスト: pytest（healthz、SSE ping、auth無効時/有効時の挙動）。lintはruff

### 完了条件

- [ ] `ruff check` / `pytest` クリーン（ローカル — 本タスクの完了範囲）
- [ ] （INFRA-01 apply後）コンテナ化しOCIR push → CI起動 → API GW経由で `/healthz` と `/api/chat/ping` のSSEが応答
- [ ]（INFRA-02後）`AUTH_REQUIRED=true` で実JWTによる認証付き応答

### 成果物

- `packages/api/` 一式、`docs/verification/APP-01.md`（ローカルテスト結果。API GW疎通はINFRA-01 apply後に追記）

### 禁止事項

- OCID・パスワードのコミット（設定はすべて環境変数/.env経由）

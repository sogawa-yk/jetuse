# ADR-0028: OpenAPI 仕様の公開経路と、認証が要る配備で仕様を無認証にするかの既定

日付: 2026-08-04 / 状態: **Proposed（§2 は人間の判断を仰ぐ。§1 は実装済み）** /
関連: API-01（本 ADR の起票元）, SP1-01（能力登録簿 `/api/capabilities`）,
仕様: `specs/17-demo-platform-redesign.md` §3 /
検証: `docs/verification/API-01.md`

## 背景

デモ作成者が JetUse を使うとき、機械可読な仕様が外から取れなかった。FastAPI は
`/openapi.json` を**既に生成している**が、API Gateway が CI（FastAPI）へ通しているのは
`/api/*` だけなので外から到達できない。SDK は配らない方針（契約がまだ動いており、
古い SDK が動いてしまう状態を作らない）なので、**仕様を配れる状態にすることが入口**になる。

実測（2026-08-04・配備済み us-chicago-1）:

| リクエスト | 応答 | 意味 |
|---|---|---|
| `GET /api/openapi.json` | 404 `{"detail":"Not Found"}` | **FastAPI 自身の 404** = `/api/{p*}` で CI に届いている |
| `GET /openapi.json` | 404 `ObjectNotFound ... bucket 'jetuse-…-spa'` | Gateway の SPA(Object Storage)側へ落ちる = CI に届かない |
| `GET /api/capabilities` | 200 | キャッチオール経路は生きている |

**＝ 経路は既にある。置き場所を `/api/` 配下にするだけで済み、Terraform 変更は要らない。**

## §1 公開経路（実装済み・判断不要）

- 仕様は **`GET /api/openapi.json`** で返す（`routes/spec.py`）。Gateway のキャッチオール
  `/api/{p*}` にそのまま乗るため、ゲートウェイのルート追加は不要。
- FastAPI 既定の `openapi_url` / `docs_url` / `redoc_url` は **無効化**した。仕様を返す口を
  1 本に限れば、認証が要る配備で「仕様だけ無認証で晒す経路」が残らない（fail-closed）。
- 能力登録簿との関係は**導出**で固定する。ワイヤ契約（path/method/スキーマ/パラメータ説明）の
  正本は OpenAPI、用途側（`summary` / `when_to_use` / `example` / `demo_safe` / 能力差）の正本は
  登録簿。登録簿は OpenAPI から `requestBody` / `responses` を導出しており、二重管理にしない。
  ズレは `packages/api/tests/test_openapi_spec.py`（登録簿の全 route が**配った仕様**に実在する）
  で検出する。

## §2 判断を仰ぐ点 — 認証が要る配備で仕様を無認証にするか

現状の実装は **案 A（fail-closed）**である。ここを既定として確定してよいか、判断を仰ぐ。

前提となる事実:

- `auth_required=false` の配備（開発用 dev スタック・現在の検証環境）では、そもそも API 全体が
  無認証なので、仕様の公開で**新たに増える露出は無い**。
- 公開ワンクリックスタック（`infra/orm`）は **`enable_auth=true` が既定** → `AUTH_REQUIRED=true`。
  ここが論点の本体。
- 仕様には**裏方ルートも載る**（`/api/admin/*` `/api/conversations/*` `/api/agent/mcp-servers` 等）。
  能力登録簿は `demo_safe=true` の 8 能力だけを見せるが、OpenAPI は全ルートの地図である。
- JetUse を**呼ぶ**にはいずれトークンが要る。仕様だけ無認証にしても、デモ作成者が
  「認証なしで開発を始められる」わけではない。

| 案 | 内容 | 得るもの | 失うもの |
|---|---|---|---|
| **A（実装済み・推奨）** | 仕様は他の `/api/*` と同じ `require_user` を通す。`AUTH_REQUIRED=true` なら仕様も Bearer 必須 | 攻撃面の地図（裏方ルート含む全ルート）を未認証者に配らない。設定が増えない | 公開スタックで「まず仕様だけ見たい」人は先にトークンが要る（呼ぶには要るので実害は小さい） |
| **B** | 仕様は常に無認証で返す | 誰でも `curl` 一発でカタログを取れる。生成器を回す導線が最短 | 認証付き配備でも全ルート・全スキーマ・全パラメータが公開される。裏方ルートの存在が読める |
| **C** | 既定は A、`OPENAPI_PUBLIC=true` を明示した配備だけ無認証（オプトイン） | 社内向けポータル等で無認証カタログを出せる | 設定が 1 つ増える＝**誤設定で公開しうる**。fail-closed の性質はスイッチ 1 つ分だけ弱まる |

**推奨は A。** 理由は 3 つ:
(1) 仕様が要る人は API も呼ぶので、トークンの有無で導線はほとんど変わらない。
(2) OpenAPI は能力登録簿と違い**裏方まで含む全ルートの地図**であり、公開の副作用が大きい。
(3) C は必要が生じた時点で A の上に足せる（今入れる必要が無い＝設定を増やさない）。

C を選ぶ場合に必要な追加実装（本タスクでは**実装しない**）: `Settings` に `openapi_public`
を足し、`routes/spec.py` の依存を「`openapi_public` なら認証省略」に分岐、
`AUTH_REQUIRED=true` かつ `OPENAPI_PUBLIC=true` の組み合わせを起動ログに警告として出す。

## 決定

§1 は実装済み。§2 は **未決**（人間ゲート）。A で確定した場合は本 ADR を Accepted にし、
`specs/17` の当該記述をそのままとする。B / C を選ぶ場合は本 ADR を差し戻し、
別タスクで実装する（本タスクでは案 A のまま止める）。

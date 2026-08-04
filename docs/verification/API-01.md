# API-01 検証: OpenAPI 仕様を外部へ公開する

日付: 2026-08-04 / 環境: 配備済み app スタック（us-chicago-1・`AUTH_REQUIRED=false`）+ ローカル実 HTTP /
証跡: `runs/2026-08-04T1033_API-01/e2e/` / 判断待ち: `docs/decisions/ADR-0028-openapi-exposure-and-auth.md`

## 結論

- 仕様は **`GET /api/openapi.json`** で返る。**Terraform 変更は不要**だった（Gateway の既存
  キャッチオール `/api/{p*}` にそのまま乗る）。実測でルーティングを確認している（下記 1）。
- 取得した仕様（OpenAPI **3.1.0** / 57 path / 29 schema）から **クライアントを生成して配備済み
  JetUse を実際に呼べた**（`openapi-python-client` 0.29.0・手書きコード無し）。**SDK は作らない**
  判断（契約が動いている間は生成のほうが安全）が成立することを実証した。
- **仕様だけを渡したエージェントが、文書検索付きエージェント実行を 1 回目で正しく呼べた**
  （下記 7）。同じ課題を**変更前の仕様**で与えた対照群は HTTP 200 を得たものの
  `enabled_tools` を指定できず、**検索せずに `code_interpreter` を起動した**＝「動いたが
  頼んだことはしていない」。差は HTTP ステータスではなく**正しく呼べたか**に出た。
- 実際に人が詰まった 3 点（A: `agent`×`rag` の排他 / B: エージェントの文書検索に
  `enabled_tools=["rag_search"]` が要る / C: `rag_backend` と `agent_rag_backend` の違い）は
  **仕様の説明文から読める**ようにし、**その文言どおりに実環境が 400 を返すことを実測**した。
- 認証が要る配備で**仕様だけ無認証で晒さない**（`AUTH_REQUIRED=true` で 401）。
  公開配備での既定は ADR-0028 §2 で**判断を仰ぐ**（実装は fail-closed のまま止めてある）。

## 1. なぜ `/api/` 配下なのか（実測 — 推測ではない）

| リクエスト（配備済み） | 応答 | 読み取れること |
|---|---|---|
| `GET /api/openapi.json` | 404 `{"detail":"Not Found"}` | **FastAPI 自身の 404** = `/api/{p*}` で CI に届いている |
| `GET /openapi.json` | 404 `ObjectNotFound … bucket 'jetuse-…-spa'` | Gateway の SPA(Object Storage) 側へ落ちる = **CI に届かない** |
| `GET /api/capabilities` | 200 | キャッチオール経路は生きている |

＝ **生成はされていて経路も既にある。置き場所だけの問題**だった（チケットの前提どおり）。
ルート直下に置くと Object Storage に吸われるため、`/api/` 配下が唯一素直な選択になる。

## 2. 実装（最小）

- `service/routes/spec.py`: `GET /api/openapi.json`。他の `/api/*` と同じ `require_user` を通す。
- `service/main.py`: FastAPI 既定の `openapi_url` / `docs_url` / `redoc_url` を **無効化**。
  仕様を返す口を 1 本に限る＝認証が要る配備で**無認証の抜け道を残さない**（fail-closed）。
  併せて `info.description` に「取得方法・登録簿との役割分担・つまずきやすい点への導線」を書いた。
- `service/schemas.py`: `ChatRequest` の主要パラメータに `description`（**何であるかではなく
  「いつ使うか」**）と `examples` を付けた（**契約は変えていない** — 型・必須性・パラメータ名はそのまま）。
- `service/routes/chat.py`: `POST /api/chat/stream` の説明に**事前に 400 になる 3 つの組み合わせ**を
  エラー文言つきで明記。
- `service/openapi_errors.py`（新規）: **エラー応答を仕様に載せる**。コードの意味
  （400/404/409/413/422/503 = 何が起きたか・どう直すか）を **1 か所**に持ち、各ルートは
  `responses=error_responses(...)` で参照してルート固有の事情だけを足す（文言を書き写さない）。
  応答形は `ErrorResponse`（`{"detail": "<理由>"}`）。**422 だけは実装が 2 通り返す**ので
  `oneOf: [HTTPValidationError, ErrorResponse]` で宣言する（スキーマ検証で落ちれば
  `detail` は項目ごとの配列、ルート側の検証で落ちれば文字列）。**片方だけ宣言すると生成
  クライアントが実応答を解けない** — 当初は配列形だけを載せていて review-6 で指摘された。
  実応答との一致は実環境で実測した（§4 の下段）。
  載せたルート: `POST /api/chat/stream` / `POST /api/rag/files` /
  `POST /api/agent/execute-tool`（409 あり）/ `POST /api/minutes/{mid}/generate`（409 あり）。
  **実装が返しうるコードを漏らさない**のが要点で、当初 400/404/409/413/422 だけを載せていたが
  review-7 で「`_rag_call()` の 502（上流の OCI GenAI エラー）と DB 到達不能の 503 が漏れている」と
  指摘され、4 ルートに 502/503 を追加した（漏らすと `raise_on_unexpected_status=True` の生成
  クライアントが障害時に型のない例外で落ちる）。実挙動との一致は単体で固定
  （`test_declared_503_matches_the_db_outage_response` / `..._502_...`）。
  さらに review-9 で **401 の宣言漏れ**（`require_user` を通る全 71 ルート。`AUTH_REQUIRED=true` の
  公開スタックでは常態）と、**SSE ルートの 200 が `application/json` と宣言されていた**ことを
  指摘され、両方是正した（前者は router 単位で共通宣言、後者は `SSEResponse`（`media_type` だけを
  持つ `StreamingResponse` の派生）で 6 ルートの 200 を `text/event-stream` に）。
- `service/routes/rag.py`: アップロードの操作説明に**いつ使うか＋ curl の実例**（`attributes` 付き）。

### 能力登録簿との二重管理を作らない

| | 正本 | 導出 |
|---|---|---|
| ワイヤ契約（path/method/スキーマ/パラメータ説明） | **OpenAPI** | 登録簿が `requestBody`/`responses` を引く |
| 用途（`summary`/`when_to_use`/`example`/`demo_safe`/能力差） | **能力登録簿** | — |

ズレ検出は `packages/api/tests/test_openapi_spec.py`
（登録簿の全 route が **HTTP で配った仕様**に実在すること）と既存 `tests/test_capabilities.py` の 2 段。
前者を HTTP 経由にしたのは、in-process の `app.openapi()` だけを見ると**公開経路が壊れていても
検査が通ってしまう**ため。

## 3. 仕様から生成したクライアントで実際に呼ぶ（シナリオ2）

```
openapi-python-client generate --path openapi.json --meta none --output-path gen
python call_generated_client.py https://<APIGW-HOST>
→ GET /api/chat/models   200 / モデル 11 件
→ GET /api/capabilities  200 / 能力 8 件
```

さらに**中心の SSE 経路**も生成クライアントで呼べることを確認した（当初は GET 2 本だけで、
主経路を通していなかった — review-9 F-001）:

```
python call_generated_client_sse.py https://<APIGW-HOST> <gen の親>
→ 200 / content-type: text/event-stream / 本文 52 フレーム → data: [DONE] に到達
```

証跡 `runs/2026-08-04T1033_API-01/e2e/scenario-2-*`（生成物の関数を呼ぶだけのスクリプト同梱）。

## 4. 説明文が実装とズレていないこと（シナリオ3）

配備済み環境へ実際に投げ、**仕様に書いた文言と同じ 400** が返ることを確認した。

| 投げたもの | 実結果 |
|---|---|
| `agent=true, rag=true` | 400 `agent and rag cannot be combined` |
| `agent=true, agent_rag_backend=adb, enabled_tools=[]` | 400 `agent_rag_backend requires rag_search in enabled_tools` |
| `agent_rag_backend=adb`（agent 無し） | 400 `agent_rag_backend requires agent mode` |
| `rag_filters` のみ | 400 `rag_filters requires rag=true` |

**エラー応答の形も実環境で実測した**（`scenario-5-error-shapes.md`）。**422 は実際に 2 形ある**:

| 投げたもの | HTTP | `detail` |
|---|---|---|
| `model` 欠落（スキーマ検証） | 422 | **配列**（`loc`/`msg`/`type`） |
| `images` + `agent`（ルート側の検証） | 422 | **文字列** `images cannot be combined with agent/rag` |
| アップロードで `.exe` | 422 | **文字列** `unsupported file type '.exe'. allowed: …` |
| 存在しない `agent_id` | 404 | 文字列 `agent not found` |

409 / 413 / 503 は実環境で起こしていない（理由は `scenario-5-error-shapes.md` 末尾。409 は
`tests/test_http_tools.py` で単体固定済み）。

契約非変更の機械的証明（`scenario-3-contract-diff.json`）: **要求側**（`requestBody` /
`parameters` / `security`）の差分ゼロ・削除ゼロ。既存コードで宣言が変わったのは
**422 の 3 ルートだけ**で、これは上記のとおり**実装に合わせる修正**（実装の挙動は不変）。
他は追記のみ＝①仕様公開ルート 1 本 ②既に返っていた 4xx/503 の記述 ③`ErrorResponse` schema。
副産物として、生成クライアントに `error_response.py` / `http_validation_error.py` が生まれ、
422 は `_parse_response_422() -> ErrorResponse | HTTPValidationError` として**両形を解ける**。

## 5. チェック

- `.venv/bin/pytest packages/api/tests` → **945 passed**（証跡 `e2e/pytest-full.txt`）
- `.venv/bin/ruff check packages/api` → **All checks passed**
- `infra/` は未変更のため `ops/check-infra.sh` の対象外（Gateway 変更なし）

## 6. 未実施（理由あり）

`runs/2026-08-04T1033_API-01/e2e/SKIPPED.md` に 3 件。要点:

1. **変更後イメージを配備した状態での取得**は `terraform apply`（人間ゲート・本セッションでは禁止）
   が要るため未実施。代替として「配備済み Gateway で当該パスが FastAPI に到達していること」と
   「変更後コードが実 HTTP で 200 を返すこと」を別々に示した。人間が行う手順は SKIPPED.md に記載。
2. `AUTH_REQUIRED=true` で**正当なトークン**での 200 は、ローカルに OIDC 発行元が無いため未実施
   （無認証で **401** になることは実 HTTP で確認済み。禁止事項はこちら側）。
3. Gateway ルート追加の `terraform plan` は**不要だったため無し**（`infra/` 未変更）。

## 7. コーディングエージェントは仕様だけで正しく呼べるか（実証・シナリオ4）

**この API の最も重要な利用者はコーディングエージェント**である（人は 400 を食らってから
試行錯誤できるが、**エージェントは仕様に無いことを推測＝でっち上げる**）。追加指示（2026-08-04）を
受けて、実際に測った。証跡: `runs/2026-08-04T1033_API-01/e2e/scenario-4-agent-with-spec-only.md`。

**実験**: 被験者はサブエージェント 2 体。渡すのは `openapi.json` 1 ファイルと base URL だけ
（リポジトリ・Web・事前知識の使用を禁止。プロンプトは 2 体で同一）。課題は
**「エージェントに手元の文書を検索させるチャットを呼んで成功させる」**。HTTP 呼び出しは
こちらが用意したラッパ経由に限定し、**記録は自己申告ではなくラッパが書いたログ**を一次証跡とした。

| | A 群（本タスク後の仕様） | B 群（対照＝変更前の仕様） |
|---|---|---|
| 1 回目の HTTP | **200** | **200** |
| 送った鍵のパラメータ | `agent`, `auto_tools`, **`enabled_tools=["rag_search"]`** | `agent`, `auto_tools`, `agent_rag_backend`, `max_tool_hops`（**`enabled_tools` 無し**） |
| 実際に起動したツール | **`rag_search`**（確認呼び出し・下記） | **`code_interpreter`** |
| 課題の達成 | **達成** | **未達**（検索の口を開けていない） |

**HTTP では差が出ず、差は「正しく呼べたか」に出た。** B 群は 200 を得たが文書検索は行われず、
`code_interpreter` が起動した＝**動いたが頼んだことはしていない**。B 群自身の記述:
「`enabled_tools` は enum も名前の例も無いためツール名を書けなかった（null=全ツール有効と推測）…
**『文書を検索させる』経路に確実に載せる手立ては spec からは読み取れなかった**」。
A 群の根拠は本タスクで足した記述（ルート説明の排他・依存、`ChatRequest.examples` の
「エージェント実行（文書検索あり）」、`auto_tools` の「無人で回すなら true」）だった。

**A 群の形が実際に検索を起こすことの確認**（実行者はこちら。A 群の課題は `max_tokens ≤ 64` の
制約で本文が空になったため、同じ形のまま 512 で 1 回呼び直した）:
SSE に `tool_call{name=rag_search}` が 1 件・`citations` 1 件・本文 173 文字。
**＝ 仕様に書いた形が実環境で文書検索を起こし、出典付きで答える。**

**正直に書く限界**: ①B 群も 200 を取ったので「対照は失敗」という単純な数字は出ていない
（n=1 ずつなので成功率のような指標は作らない）。②隔離は指示ベースで強制ではない
（`claude -p` による別プロセス隔離も試したが、この環境では未ログインで実行できなかった）。
ただし B 群が「`rag_search` という名前に到達できなかった」こと自体が、docs を読んでいない裏付けになる。
③両群が共通して推測に回した点が 2 つ残る: **`model` の有効値**（仕様は
`GET /api/chat/models` を指すが、そこを呼べない条件では確定できない。モデル登録簿を
schema に転記すると二重管理になるため enum にはしない）と、**トークンの取得方法**
（→ 本タスクで `info.description` に「この API に発行の口は無い / 配備側から受け取る」を追記した）。

## 8. Tips（他タスクへの申し送り）

- **`.env` をリポジトリ直下に置いたまま `pytest` を回すと固まる**。`Settings` が CWD の `.env` を
  読むため、単体テストが実 ADB / 実 OCI を掴もうとする（実測: 20 分経っても終わらない。
  CI は `.env` 無しで `pytest -q`）。E2E で `.env` が要る場合はテストの前後で退避する。
- FastAPI の `docs_url` は `openapi_url` が有効なときだけ有効になる。仕様の口を認証付きの
  自作ルート 1 本に寄せると Swagger UI（`/docs`）は自動では出せない。必要になったら
  `get_swagger_ui_html` を同じ認証の下に置く（本タスクでは要件外のため作っていない）。

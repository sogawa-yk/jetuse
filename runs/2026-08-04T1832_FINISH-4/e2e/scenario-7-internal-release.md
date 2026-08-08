# E2E-7: Internal リリース点の統合確認

対象: `internal-dev` = `ee142e5`（`internal-v0.1.0` の候補点）
環境: 実 OCI / `jetuse:dev` / **us-chicago-1** / 自分の app スタック（`jetuse-sogawa-api`）
モック不使用。実際に配備して HTTP を叩いている。

## なぜ必要だったか

`internal-stable` は 2026-07-06 から止まっており、PR #132 で追随させたときに
「リリース単位の E2E が未実施」としてタグを保留していた。稼働していた Internal 環境
（共有 `jetuse-dev-app`）は **146コミット遅れ**の `6cf875d`（2026-07-23）で、
AGT-04/05/06・TOOL-01/02/03・PREP-03 等を含んでいなかった。**そこでの E2E は
リリース点を検証しない。** そのため、リリース点のコードで配備し直した。

## 配備

| 項目 | 結果 |
|---|---|
| ビルド | `engine=docker platform=linux/amd64`（PR #139・#144 の成果） |
| 稼働イメージ | `jetuse-dev-api:dev-sogawa-ee142e5` = `internal-dev` の HEAD と一致 |
| terraform | `Apply complete! Resources: 1 added, 1 changed, 0 destroyed.` |
| URL | API Gateway（us-chicago-1）で公開 |

## 疎通と機能

| 経路 | 結果 |
|---|---|
| `GET /` | 200 |
| `GET /api/health` | 200 |
| `GET /api/chat/models` | 200 / **11モデル** |
| `GET /api/capabilities` | 200 |
| `GET /api/chat/ping`（SSE） | ストリーム受信 OK |

### 実推論（AGT-06 のシカゴ移行の成果を実測）

```
POST /api/chat/stream  model=gpt-oss-120b  "1+1は？数字だけ"
  data: {"delta": "2"}   usage: in=74 out=54   [DONE]

POST /api/chat/stream  model=grok-4.3       "1+1は？数字だけ"
  data: {"delta": "2"}   usage: in=199 out=109  [DONE]
```

**Grok 系が使えるのはシカゴ移行(AGT-06)の成果。** 大阪では利用できなかった（ADR-0001）。
移行前の稼働環境ではモデル5本だったが、リリース点では11本になっている。

### capabilities の内訳

| 機能 | 状態 | 判定 |
|---|---|---|
| chat | ok（11モデル） | PASS |
| rag | ok | PASS |
| dbchat | ok / `select_ai=true` | PASS |
| ocr | ok | PASS |
| tts | ok / `region=us-phoenix-1`（フォールバック動作） | PASS |
| speech | unavailable / `SPEECH_BUCKET 未設定` | **設定由来**（不具合ではない） |
| agents | disabled / ホスト型未配備・`auth_required=false` | **設定由来** |
| dbchat.semantic_store | false / `SEMSTORE_OCID 未設定` | **設定由来** |

`/api/health` の集約 `ok` は `false` になるが、これは**未設定の任意機能を含めた集約**であり、
このスタックの構成どおり。回帰ではない。

### 内部固有コードが配備されていることの確認

| 経路 | 結果 | 意味 |
|---|---|---|
| `GET /api/builder/sessions` | **405** Method Not Allowed | ルートは登録済み（SP3 ビルダーのコードが入っている） |
| `GET /api/demos` | 503 `database unavailable` | 下記 |

## デモ基盤（SP1〜SP3）の統合 E2E — 追試で合格

初回は `/api/demos` が 503 `database unavailable` で確認できなかった。原因は
**個人スキーマに内部固有 migration が未適用**だったこと（`main` の checkout から
`migrate` を流していたため、内部固有の `017_demos_v2` 以降がそもそも存在しなかった）。

`internal-dev` の checkout から、アプリが使う**シカゴの `jetuse-dev-adb`** に対して
流し直した（ウォレットは Object Storage の `adb_wallet.zip.b64` から取得）。

```
接続先 DB=..._JETUSEDEV  USER=JETUSE_SOGAWA
適用: 11 件
  + 017_demos_v2  018_demos_idx_owner  019_demos_idx_visibility
  + 020_conversations_demo_id  021_conversations_idx_demo  022_demo_backend_targets
  + 023_dbt_idx  024_rag_files_filename_char
  + 025_builder_sessions  026_builder_sessions_idx  027_builder_sessions_sufficient
```

### 結果

| # | 確認 | 実測 | 判定 |
|---|---|---|---|
| 7a | `GET /api/demos`（DB 経路） | **200** `{"demos":[]}` | PASS |
| 7b | `POST /api/builder/sessions` | 200 / セッション生成 | PASS |
| 7c | 生成したセッションを DB から読み戻す | 200 / id 一致・`status=hearing` | PASS |
| 7d | `POST .../messages`（**LLM 構造化出力**） | 200 / 自然言語から `use_case` `capabilities_hint` を抽出 | PASS |
| 7e | 必須項目の不足を仕様どおり弾く | 409 `要求サマリが設計に足りません(missing: industry)` | PASS |
| 7f | 追加ヒアリングで `sufficient=true` | 200 / `industry=製造業` | PASS |
| 7g | `POST .../design`（**デモ設計**） | 200 / `plan_version` `title` `capabilities` `screens` `data` | PASS |
| 7h | 設計結果の永続化 | 200 / `status=designed`・`plan` 保存・transcript 4件 | PASS |

**7e が重要。** 必須項目が欠けた状態で設計へ進もうとすると、`specs/19 §3.1` を引用して
409 で止まる。仕様どおりのガードが実環境で効いている。

**SP3 ビルダーの中核（ヒアリング → LLM 構造化 → 決定的再検査 → 永続化 → 設計）が
実 OCI 上で完走した。** これをもってデモ基盤の統合 E2E を合格とする。

### 一次証跡

要約ではなく**実行コマンドとレスポンス本文**を `e2e/demo-platform/` に残した。
同一セッション ID で 7a〜7h が繋がっていることを追える。

- `demo-platform/README.md` — 実行コマンド全文・HTTP ステータス一覧・適用した migration
- `demo-platform/7a-demos.md` 〜 `7h-final-session.md` — 各リクエストのレスポンス本文

適用前は `/api/demos` が 503 だった。**migration の適用が状態を変えた**ことが、
この migration 群が実際に必要だったことの裏づけになっている。

### この追試で分かったこと

`make deploy` は app スタックを作るが、**内部固有 migration は流さない**。
`ops/setup-dev-schema.py` / `python -m jetuse_core.migrate` は
**実行している checkout の migrations ディレクトリ**を見るため、`main` から流すと
内部固有分が入らない。**Internal 環境を作るときは internal 系の checkout から流すこと。**

## 途中で見つけて直した配備経路の欠陥（2件）

この E2E に到達するまでに、配備経路が2箇所壊れていた。どちらもローカル
（Apple Silicon / docker）だけが踏む経路で、CI（ubuntu / x86）では露見しなかった。

1. `podman` 直書き → `podman: command not found`（PR #139）
2. `--platform` 未固定 → arm64 イメージを Container Instance が拒否。**しかも置換なので
   旧インスタンスは削除済みで、環境が落ちたまま復旧できなかった**（PR #144）

加えて、`ops/dev-env-up.sh` の namespace フォールバックが `set -e` + `pipefail` で
到達不能だった（PR #142）。**E2E を取ろうとしたこと自体が、3件の実バグを見つけた。**

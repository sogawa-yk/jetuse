# jetuse_shared

JetUse の **API(`packages/api/jetuse_core`)** と **各SDKホスト型エージェントコンテナ
(`packages/agent-containers`)** で二重管理していた**セキュリティ要件ロジック**を一本化した
共有パッケージ。リファクタリング計画 `docs/refactoring/review-validation.md` §4 (P1b) に対応。

## 収録範囲(意図的に最小)

| モジュール | 内容 | 旧二重実装 |
|---|---|---|
| `jetuse_shared.webtools` | SSRFガード `assert_public_host` / `SsrfBlockedError`、`extract_url`、`web_fetch`、`web_search`(DuckDuckGo HTMLパーサ `_DdgParser`)、`get_current_time` | `jetuse_core/webtools.py`・`jetuse_core/tools.py` ⇔ `agent_common.py` |
| `jetuse_shared.sqlguard` | `sanitize_sql`(SELECT/WITH限定ガード) + `_BANNED` 正規表現 + `SqlRejectedError` | `jetuse_core/nl2sql.py` ⇔ `agent_db.py` |

### 共有しなかったもの(意図的に各ランタイムへ残置)

- **SemanticStore 生成 / NL→SQL 生成 / 読取専用プール(`execute_readonly`)**:
  pydantic Settings(API) と os.environ + resource principal ウォレット取得(コンテナ)で
  接続・認証経路が正しく異なる。共有すると「コンテナは薄い per-runtime requirements」という
  ADR-0009 の設計意図を壊すため**据え置き**(将来共有する場合は別タスク)。
- **pydantic `Settings`**: ランタイム固有。`jetuse_shared` は pydantic にも os.environ にも依存しない。

## 設計: 設定と例外の adapter

`jetuse_shared` はどの設定機構にも依存しない。可変パラメータは**関数引数 / 軽量 dataclass `FetchConfig`**
で受け取り、各ランタイムが値を注入する。

- **API側(`jetuse_core`)**: pydantic `Settings` から値を渡し、`SsrfBlockedError`/`SqlRejectedError` を
  `jetuse_shared` から再エクスポートして従来の公開APIを維持(`jetuse_core.webtools.SsrfBlockedError`
  などを import している既存コード/テストはそのまま動く)。
- **コンテナ側(`agent_common.py`/`agent_db.py`)**: os.environ で値を組み立てて渡す。
  旧コンテナ実装は SSRF / SQL を `ValueError` で送出していたが、`jetuse_shared` の
  `SsrfBlockedError`/`SqlRejectedError` はいずれも **`ValueError` のサブクラス**なので、
  `run_tool` 等の `except Exception` 経路は挙動不変。

## MAX_TEXT_CHARS の統一(挙動差の解消)

統一前は `web_fetch` ツールの本文上限が乖離していた:

- **API**: `extract_url` がページ抽出時に `MAX_TEXT_CHARS=20000` で打ち切り → さらに
  `tools.web_fetch_handler` が `page["text"][:8000]` で再打ち切り。**ツール出力は実効 8000 字**。
- **コンテナ**: `_extract_url` 内で `[:8000]`。**ツール出力 8000 字**。

→ 両ランタイムの **観測挙動はもともと 8000 字**だった(乖離していたのは「内部のページ抽出上限」)。
そこで `jetuse_shared` では2段の定数に分離して挙動を保存した:

- `MAX_PAGE_CHARS = 20000` … `extract_url` の戻り値 `text`(ページ抽出の生上限。API従来値)。
- `MAX_TEXT_CHARS = 8000` … `web_fetch` ツール出力の `text` 上限(**両ランタイムの実効値=正準**)。

`web_fetch(url, max_text_chars=...)` で上書き可能。API の `extract_url` エンドポイント
(`/api/tools/extract-url`)は引き続き 20000 字の `extract_url` を直接呼ぶため、こちらも挙動不変。

## インストール

```bash
# API の .venv へ editable install(リポジトリでは jetuse_core が path 依存で参照)
pip install -e packages/jetuse_shared
```

コンテナは各 `Containerfile.*` がビルドコンテキスト(リポジトリルート)から
`packages/jetuse_shared` を COPY して `pip install ./jetuse_shared` する(下記)。

## コンテナのビルド方法(ビルドコンテキスト変更)

`Containerfile.*` の `COPY` はビルドコンテキスト外へ到達できないため、`jetuse_shared` を
コンテキストに含める必要がある。本パッケージでは**ビルドコンテキストをリポジトリルートに変更**し、
各 Containerfile が `packages/agent-containers/*` と `packages/jetuse_shared` の両方を COPY する方式を採った。

```bash
# リポジトリルートから(-f でファイル指定、最後の . がルート=ビルドコンテキスト)
podman build -f packages/agent-containers/Containerfile.openai \
  -t jetuse-agent-openai packages/agent-containers/..  # = リポジトリルート相当
```

ビルドスクリプト `packages/agent-containers/build.sh` がこの3イメージビルドをまとめる。

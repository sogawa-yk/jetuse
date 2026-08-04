# 検証レポート: CI api ジョブ失敗の修正(fdk import エラー)

- 日付: 2026-06-24
- 対象: `.github/workflows/ci.yml` の `api` ジョブ / `packages/api/tests/test_fn_router.py`

## 事象

main への push で CI の `api` ジョブが `pytest` 収集段階で失敗(exit code 2)。

```
tests/test_fn_router.py:7: from fn.router import func as router
fn/router/func.py:18:    from fdk import response
E   ModuleNotFoundError: No module named 'fdk'
```

`ruff` は成功。テスト本体はDB/OCIをモックする正当な単体テストだが、テスト対象
`fn/router/func.py` が import 時に `fdk`(OCI Functions Development Kit)を参照し、
CI環境に `fdk` が無いため収集に失敗していた。

## 方針判断

`fdk` を API の dev 依存に追加する案は不採用。理由:

- `fdk` は `iso8601==0.1.12` を**完全固定**しており、本体依存 `oci` SDK と衝突しうる。
- `Cython` / `httptools` / `pbr` 等のビルド依存まで引き込み、CI/dev 環境を重くする。
- `func.py` が `fdk` から使うのは `fdk.response.Response`(レスポンス整形の薄いラッパ)のみ。

→ テスト実行時だけ `fdk` の軽量スタブを `sys.modules` に注入する方式を採用。
本番のFunctionsイメージ(`Containerfile.fn`)は実 `fdk` を使うため無変更。
スタブは実 `fdk.response.Response` の公開挙動(`body()` / `ctx.SetResponseHeaders()` 呼び出し)を再現。

## 変更

- 新規: `packages/api/tests/conftest.py` — `fdk` 軽量スタブを注入。

## 実機検証

CI と同一構成(Python 3.12 venv, `pip install -e ".[dev]"`)で実行。

```
$ ruff check .
All checks passed!

$ pytest tests/test_fn_router.py -v
7 passed in 0.43s

$ pytest -q   # 全テスト + カバレッジ下限(--cov-fail-under=45)
145 passed, 3 warnings in 8.62s
Required test coverage of 45% reached. Total coverage: 54.96%
```

→ CI `api` ジョブは解消見込み。`web` / `terraform` ジョブは元々成功。

## 残課題(本修正の範囲外)

- `release.yml` の `images` ジョブが GHCR push で `denied: permission_denied: write_package`。
  → リポジトリ/Org のパッケージ書き込み設定の問題(別途、設定手順を参照)。
- 全ジョブで Node.js 20 非推奨の警告(`actions/checkout@v4` 等)。今は動作に影響なし。

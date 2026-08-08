# E2E-5: OCIR namespace のフォールバックが死んでいた

対象: `ops/dev-env-up.sh` の namespace 解決
実 `.env` と実スクリプトに対して実施。モック不使用。

## 症状

Internal リリース点を配備しようとして、**何も出力せず rc=1 で終了**した。

```
$ ops/dev-env-up.sh sogawa
$ echo $?
1
```

## 原因

スクリプトはコメントで優先順を宣言していた。

```
# 優先順: 環境変数 > .env の OCIR_NAMESPACE > .env の OS_NAMESPACE
_ns_from_env_file() { grep "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'\''\r'; }
NS="${OCIR_NAMESPACE:-$(_ns_from_env_file OCIR_NAMESPACE)}"
[ -n "$NS" ] || NS="$(_ns_from_env_file OS_NAMESPACE)"     # ← ここに到達しない
```

`set -euo pipefail` 下で `grep` の「一致なし」(rc=1) が `pipefail` によりパイプライン全体の
失敗になり、**代入の時点で `set -e` が発火してスクリプトごと終了**する。
つまり `OS_NAMESPACE` フォールバックは**到達不能な死にコード**だった。

この開発機の `.env` には `OS_NAMESPACE` しかない（`OCIR_NAMESPACE` は 0 件）ため、常に踏む。

## 再現

```
$ bash -c 'set -euo pipefail
_ns() { grep "^$1=" .env | tail -1 | cut -d= -f2- | tr -d "\"'\''\r"; }
NS="${OCIR_NAMESPACE:-$(_ns OCIR_NAMESPACE)}"
echo "ここには到達しない"'
$ echo $?
1
```

## 修正と確認

`_ns_from_env_file` の末尾に `|| true` を足して、空振りを成功扱いにした。

```
$ bash -x ops/dev-env-up.sh sogawa | grep '^+ NS='
+ NS=
+ NS=<namespace>     ← フォールバックに到達した
```

| ケース | 期待 | 実測 |
|---|---|---|
| `.env` に `OCIR_NAMESPACE` | それを使う | PASS |
| **`.env` に `OS_NAMESPACE` だけ** | フォールバック | PASS |
| 環境変数 `OCIR_NAMESPACE` | 最優先 | PASS |
| どちらも無い | 空（呼び出し側が落とす） | PASS |
| 引用符・CRLF 付き | 値だけ取る | PASS |

テストは**スクリプト本体から該当ロジックを正規表現で抜き出して**実行する。
コピーすると実装が変わったときに嘘になるため。

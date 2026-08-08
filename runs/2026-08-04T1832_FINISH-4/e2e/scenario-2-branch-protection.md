# E2E-2: branch protection（ruleset）

対象: GitHub ruleset `long-lived-branches` (id=20368467) / 実リポジトリ `sogawa-yk/jetuse`
モック不使用。実際に push を試して拒否されることを確認している。

## 適用内容

| 項目 | 設定 |
|---|---|
| 対象 | `main` / `public-dev` / `internal-dev` / `internal-stable` |
| PR 必須 | あり（approval 0件 — 単独メンテナのため） |
| CI 必須 | `api` / `web` / `terraform` / `scan` / `branch-base` |
| force push | 禁止（`non_fast_forward`） |
| 削除 | 禁止（`deletion`） |
| merge method | merge commit のみ |
| bypass | **なし** |

## 2a: 保護対象への direct push が拒否される

```
$ git push origin HEAD:public-dev
remote: error: GH013: Repository rule violations found for refs/heads/public-dev.
 ! [remote rejected] HEAD -> public-dev (push declined due to repository rule violations)
rc=1
```

**PASS。** ADR-0028 の「`main` は release PR でのみ更新する」が規約から強制へ変わった。

## 2b: bot の直接 push を無くしたこと

個人所有リポジトリでは ruleset の `bypass_actors` に GitHub Actions を指定できない
（`Actor GitHub Actions integration must be part of the ruleset source or owner organization`）。
そのため release workflow の dist commit ステップを削除し、`ci.yml` の web ジョブで
陳腐化を検出する方式に変えた（ADR-0030）。

適用時点の実測: コミット済み `packages/web/dist` は macOS 上の再ビルド結果と**バイト一致**。

新しい dist ガードの3経路を実測した（正常系だけでなく**失敗経路も**）。

| 経路 | 操作 | `git diff` のみ（旧案） | 採用した `git status --untracked-files=all` |
|---|---|---|---|
| 正常系 | 何もしない | — | `FRESH` rc=0 |
| A: 追跡済みの陳腐化 | `index.html` に追記 | 検出 | `STALE / M packages/web/dist/index.html` rc=1 |
| B: **新規未追跡のみ** | `assets/_new-chunk-abc123.js` を作成 | **検出できず（素通り）** | `STALE / ?? …_new-chunk-abc123.js` rc=1 |

**B が重要。** ビルドが新規ファイルしか生まないケースでは `git diff` は差分を返さず、
「dist は最新」と誤って通していた。`--untracked-files=all` に変えて塞いだ。
（`.gitignore` が `!/packages/web/dist/` で除外解除しているので `??` として現れる）

未検証: **release workflow 実行後にブランチ HEAD が更新されないこと**。dist commit ステップを
削除した効果は、次に `main` へ release PR を merge したとき（4件目の Public リリース）に確認する。

## 2c: PR 経由なら通ること

**この PR 自身が検証になる。** 保護下の `public-dev` に対し、PR + CI 必須を満たして
マージできることをもって確認する（結果は下記に追記）。

**確定した（2026-08-04）。** protection 適用後、以下がすべて PR 経由・CI 必須を満たして
マージされた。direct push は拒否されるが、PR 経由なら通ることが実運用で確認できている。

| PR | 宛先 | 結果 |
|---|---|---|
| #137 | public-dev | MERGED（protection 適用直後の1本目） |
| #139 | public-dev | MERGED |
| #141 | internal-dev | MERGED |
| #142 | public-dev | MERGED |
| #143 | internal-dev | MERGED |
| #144 | public-dev | MERGED |
| #138 | **main** | MERGED（Public リリース。安定枝も PR 経由で通る） |

`#138` が重要 —— **保護した `main` にも release PR なら入る**ことの確認。

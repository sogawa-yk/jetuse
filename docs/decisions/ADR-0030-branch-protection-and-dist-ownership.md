# ADR-0030: branch protection の適用と、dist を人の成果物にする

- Status: Proposed（**承認は人間ゲート**）
- Date: 2026-08-04
- 関連: ADR-0028（4ブランチ体制）

## Context

ADR-0028 で4ブランチ体制に移行したが、**強制するものが何も無かった**。ruleset は0件で、`main` を含む全ブランチへ direct push できる状態が続いていた。「`main` = Public 安定版で release PR でのみ更新する」という前提は、規約に書いてあるだけでは保たれない。

適用しようとして、2つの制約に当たった。

### 1. GitHub Actions を bypass 対象にできない

`release.yml` の最終ステップが `packages/web/dist` を `main` へ直接 commit していた（`chore(spa): rebuild dist for ORM deploy`）。PR 必須ルールを入れるとこの push が落ちる。

ruleset の `bypass_actors` に GitHub Actions を指定しようとしたが拒否された。

```
Actor GitHub Actions integration must be part of the ruleset source or owner organization
```

このリポジトリは **個人所有**（org ではない）ため、Integration 型の bypass actor を使えない。

### 2. 単独メンテナは自分の PR を approve できない

`docs/guides/branching-and-releases.md` は review 必須を推奨していたが、`required_approving_review_count >= 1` にすると全 PR が永久に止まる。

## Decision

1. **`main` / `public-dev` / `internal-dev` / `internal-stable` に ruleset を適用する。**
   - PR 必須（direct push 禁止）
   - CI 必須（`api` / `web` / `terraform` / `scan` / `branch-base`）
   - force push（`non_fast_forward`）と削除（`deletion`）の禁止
   - merge method は merge commit のみ
2. **`packages/web/dist` を「人がコミットする成果物」に変える。** release workflow の dist commit ステップを削除し、代わりに **`ci.yml` の web ジョブが陳腐化を検出**する。判定は `git status --porcelain --untracked-files=all -- packages/web/dist` が空であること —— `git diff` だけだと**新規ファイルしか生まないビルドを素通りさせる**。bot の直接 push が無くなるので bypass が不要になる。
3. **approval は 0 件必須とする。** 単独メンテナのため。PR 経由の強制と CI 必須で担保する。あわせてガイドの「owner review 必須」「CODEOWNERS review」の記述を撤回する（approval 0 では成立しないため、書いてあるだけの規約になる）。

`dist` を追跡対象から外す選択は採らない。`.gitignore` が `!/packages/web/dist/` で明示的に追跡しており、`scripts/package-orm-stacks.sh` が `packages/web/dist/index.html` の存在を必須チェックしている（npm build なしで ZIP を作れることが目的）。

## Consequences

- 4ブランチ体制が**規約から強制へ**変わる。`main` へ feature を直接 merge できなくなる。
- dist が古いまま PR を出すと CI が落ちる。**`npm --prefix packages/web run build` の結果をコミットする**のが新しい手順。適用時点で、コミット済み dist は macOS 上の再ビルド結果とバイト一致していることを確認済み。
- release workflow は `orm-main` リリースへの zip upload だけを行う（ブランチへの push はしない）。

### 受容する residual

- **approval 0 件。** レビューの強制は効かない。Codex レビュー（`.claude/skills/codex-review`）が実質的な checker であり、これは CI の外にある。
- **dist の再現性に依存する。** CI（ubuntu / Node 22）とローカル（macOS / Node 22）でビルド出力が一致することを前提にしている。依存の更新等で差が出ると CI が落ちる。**その場合は再ビルドしてコミットすれば直る**（fail-closed 側なので、静かに壊れることはない）。
- **個人所有のままでは bot の bypass を使えない。** 将来 org へ移す場合はこの制約が消えるので、設計を見直せる。

## Alternatives considered

**ruleset の対象から `main` を外す** — bot の push は通るが、`main` への direct push を止められず ADR-0028 の中核が担保できない。

**`RepositoryRole`（write）を bypass に指定** — GITHUB_TOKEN は通るが、書き込み権限を持つ全員が bypass できるため保護の意味が無くなる。

**dist を追跡対象から外す** — bot push も陳腐化検出も不要になるが、`scripts/package-orm-stacks.sh` が動かなくなる（npm build を常に要求することになり、ORM zip の生成手順が重くなる）。

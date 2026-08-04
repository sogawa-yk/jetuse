# ADR-0028: 4ブランチ体制と起点判定の機械化

- Status: Proposed（**承認は人間ゲート**。実装・文書は同じ PR に含むが、`public-dev` の作成と
  `dev` → `internal-dev` のリネームは未実施。承認後に Accepted へ変える）
- Date: 2026-08-04
- Supersedes: ADR-0014 の Decision 1〜4（リリースラインの部分のみ。IAM / Stack 構成の決定は有効）
- Amends: ADR-0016（Internal 安定枝の存在は維持。「安定枝は Internal 側のみ」という非対称性を解消）

## Context

### `main` に「安定」の実体が無かった

`.github/workflows/release.yml` は `on: push: branches: [main]` で、`gh release upload orm-main jetuse-orm.zip --clobber` により **同じ URL の中身を毎回上書き**する。README の Deploy ボタンは `releases/download/orm-main/jetuse-orm.zip` という固定 URL を指す。したがって **`main` へ feature を merge した瞬間に公開配信物が差し替わる**。同時に `jetuse-api` / `jetuse-fn-router` / `jetuse-agent-*` の `:latest` イメージが GHCR と4リージョンの OCIR へ push される。

ADR-0014 は「Public は各利用者が tag からセルフホストするため `main` 自体が安定版で足りる」としたが、実際には成立していなかった。

| 項目 | ADR-0014 の想定 | 2026-08-04 の実測 |
|---|---|---|
| 利用者が選べる tag | リリースごとの tag | `orm-main` の1つだけ（`public-v*` は未使用） |
| tag の位置 | リリース点に固定 | 2026-07-02 で固定。`main` は135コミット先行 |
| 配信物の中身 | tag 時点で不変 | `--clobber` で毎回上書き |

実害の記録として `fix/public-deploy-oneclick`（FIX-58「Public版ワンクリックデプロイが成立していなかった9件」）がある。壊れた状態が Deploy ボタン経由で配信されていた期間があった。

### 痛みの主因は同期の向きではなく「起点の判定」だった

`dev` 固有の92コミットを調べると、大半は真に Internal 固有（SP1〜SP3 のデモ基盤）だが、**共有物が紛れ込んでいた**。

```
6632d93 chore(docs): 陳腐化 June 計画と Phase0 throwaway spikes を docs/archive へ退避
6cf875d chore(docs): verification/ を4サブディレクトリへ整理(spikes/jetuse-app/demo-platform/fixes)
```

結果、`main` は `docs/verification/` 直下に105ファイル、`dev` は直下7ファイル＋4サブディレクトリという状態になり、98ファイルの移動が Public に届いていない。`ops/sync-main-to-dev.sh` はこれを「既知の構造的乖離」として列挙し、同期のたびに手で `--ours` を選ぶ運用が固定化していた。

CLAUDE.md は既に「共有物は main 起点」と定めていたが、`loop-config.yml` の `worktree.base_branch` は `dev` であり、**ループを使うと規約に反する起点になる**という食い違いが構造的に存在した。

## Decision

1. **4ブランチ体制**にする。`main`（Public 安定）/ `public-dev`（Public 統合）/ `internal-dev`（Internal 統合。旧 `dev` をリネーム）/ `internal-stable`（Internal 安定）。
2. `main` は **`public-dev` からの release PR でのみ**更新する。feature branch を直接向けない。これにより「`main` への merge = リリース」となり、`release.yml` のトリガは変更不要のまま正しい意味を持つ。
3. **Internal ⊇ Public** を維持し、同期は `public-dev → internal-dev` の**一方向 merge 1本のみ**とする。逆向きは merge しない（merge はブランチ先端を丸ごと運ぶため Internal 固有機能が Public に漏れる）。後から公開する変更は最新 `public-dev` 上へ **cherry-pick で移植**する。
4. **起点の判定を CI で検査する**。`ops/check-branch-base.sh` が `internal-dev` 宛の PR を見て、変更が共有物のみなら落とす。内部固有パスの一覧は `ops/internal-only-paths.txt` を単一の真実源とする。強制力は CI（`pull_request` で base が渡る）側にあり、ローカルの `make lint` は base を知る術が無いため既定でスキップする（`BRANCH_BASE=` で任意実行）。
5. **ループの既定起点を `public-dev` にする**（`loop-config.yml` の `worktree.base_branch`）。CLAUDE.md の「共有物は Public 起点」と一致させる。内部固有タスクは `BASE_BRANCH=internal-dev` で上書きする。
6. **DB migration の番号帯を分ける**。Public は `0xx_`、Internal 固有は `5xx_`。既存の重複（`017`〜`021`）はリネームしない。
7. `main` は改名しない。Deploy ボタンの URL とデフォルトブランチに直撃するため、命名の対称性より安定性を優先する。

## Consequences

- Deploy ボタンの配信物は release PR を merge したときだけ変わる。開発中の状態が公開されない。
- 同期エッジは1本のまま（従来の `main → dev` が `public-dev → internal-dev` に平行移動しただけ）。release merge 2本は安定枝が開発枝の祖先なので fast-forward 相当で conflict しない。**維持コストは増えない。**
- 共有物を Internal 側に入れようとすると CI が落ちる。規律ではなく機械が守る。
- 混在 PR（内部固有＋共有物）は落とさず WARN に留める。分割を強制すると実務が回らないため。ただし共有部分が Public に届かないことは明示する。
- **分類は base 側の一覧だけで行う。** PR 側の一覧を混ぜると、検査対象の PR 自身が規則を書き換えて迂回できる（共有ファイルの接頭辞を足すだけで素通りした）。削除による迂回も同時に塞がる。代償として、新しい内部固有パスは「一覧を先に `internal-dev` へ入れる」2段階になる。
- **移行期間（base にまだ一覧が無い間）は検査が無効。** 一覧を運ぶ最初の同期 PR は「共有物のみ」の差分になるため、fail-closed にすると移行そのものを阻止してしまう。したがってこの間は SKIP する（合格表示ではなく「検査していない」と明示する）。**検査が効き始めるのは一覧が `internal-dev` に入った後**なので、移行はできるだけ早く同期まで進めること。
- **正規の同期 PR は対象外。** `public-dev` を merge しただけで独自の非 merge コミットを持たない PR は、Public の内容を運んでいるだけなので通す。ブランチ名の規約ではなく内容で判別するため、命名を変えても壊れない。
- 同じ理由で、ループの起動（`start-loop.sh` / `ensure_task_branch.sh` / `begin_stage.sh`）は base が解決できないとエラーで止まる。**`public-dev` を作る前にループを起動できない**ので、移行では「ブランチ作成」を先に済ませる。

### 受容する residual

- **内部固有コードが共有パッケージ内に同居している。** `packages/api/jetuse_core/` の `builder_*` / `demo_*` / `gen_*` / `bundle*` 等はディレクトリで切れないため、`internal-only-paths.txt` はファイル名パターンに依存している。**命名規律が崩れると検査も崩れる。** 本筋は `packages/api/jetuse_core/internal/` へ寄せることだが、影響が大きいため本 ADR では扱わない（open item）。
- **既存の migration 番号重複は残す。** `migrate.py` は `f.stem` 単位で記録するので取り違えは起きないが、同番号内の適用順はファイル名の辞書順に依存する。
- **安定枝（`main` / `internal-stable`）への誤った PR は CI では止められない。** `check-branch-base.sh` は `internal-dev` 宛だけを見る。「安定枝は release PR と hotfix のみ」は branch protection と review で担保する（本 ADR の適用後に設定する）。
- **既に `feat/<task>` 上に居る状態でフックが走ると、起点ずれの警告に到達しない。** 警告はブランチを切替・再利用する経路にのみ入れてある。

## Alternatives considered

**タグ駆動リリース（ブランチを増やさない）** — `release.yml` のトリガを `on: push: tags: ['public-v*']` に変える案。ADR-0014 の元の思想（tag 配布）に戻れてブランチが増えない。しかし `main` が「常にデプロイ可能」でなくなり、tag を打つ能動的な運用に依存する。また Internal 側と非対称なままで、規約を2回説明する必要が残る。

**共有エンハンス専用の `shared` 枝を立てて両方へ merge する** — Public / Internal が互いを含まない完全独立モデル。「両方向同期」に最も近いが、枝が5本になり、現在 Internal にしかない共有物を `shared` へ引き出す初期作業が必要。現在 Internal は実質 Public を包含しており、痛みは包含関係ではなく起点判定にあったため採らない。

**コンパートメントをブランチに対応させる** — 採らない。ブランチの軸は「公開範囲 × 安定度」、コンパートメントの軸は「権限境界 × 壊してよさ」で直交する。対応させると認知負荷が上がる。コンパートメントは別途 `dev` / `test` / `registry` の3つに整理する。

## References

- `docs/guides/branching-and-releases.md`（運用の正本）
- `ops/check-branch-base.sh` / `ops/internal-only-paths.txt` / `ops/sync-public-to-internal.sh`
- ADR-0014（Public配布のIAM選択とリリースライン）/ ADR-0016（Internal 安定リリースライン）

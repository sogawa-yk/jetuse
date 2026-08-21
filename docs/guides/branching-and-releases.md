# Public / Internal のブランチとリリース運用

JetUse は Public 版と Internal 版をどちらも正式なリリースとして扱う。Internal 版のコードが公開されても問題はないため、機密保持ではなく「安定した配布元」と「先行機能を含む統合先」の分離を目的にする。

Public / Internal の**それぞれに安定枝と開発枝を置く**（4ブランチ体制・ADR-0028）。以前は Public 側に開発枝が無く `main` が統合先を兼ねていたため、feature を merge するたびに Deploy ボタンの配信物が差し替わっていた。

## 長期ブランチ

| ブランチ | 役割 | 変更の入れ方 | OCI 配布 | リリース |
|---|---|---|---|---|
| `main` | Public 安定版。常に Deploy to Oracle Cloud 可能 | `public-dev` からの release PR **のみ** | `orm-main`リリースの専用ZIP / `:latest` イメージ | `public-vX.Y.Z` |
| `public-dev` | Public 統合。**共有物の既定の起点** | `feat/*` 等からの PR | 配信しない | – |
| `internal-dev` | Internal 統合。Public の全機能＋Internal 固有 | 内部固有の PR ＋ `public-dev` からの sync | 配信しない | – |
| `internal-stable` | Internal 安定版。施主ホストの本番が追う配信元（ADR-0016） | `internal-dev` からの release PR **のみ** | Internal の手順・環境 | `internal-vX.Y.Z` |

```text
public-dev ──── release PR ────▶ main            = Public 配信
     │
     │ sync merge（1本のみ）
     ▼
internal-dev ── release PR ────▶ internal-stable = Internal 配信
```

原則は **Internal ⊇ Public**。`public-dev` に入った変更は sync で `internal-dev` にも入り、Internal 固有機能は `internal-dev` にだけ存在する。`main` と `internal-stable` は release 先であり、同期には関与しない。

## 起点の判定 — 変更を作る前に決める

**共有物か、Internal 固有か。** これを間違えると、共有物が Internal 側に着地して Public へ届かず、両系統が乖離する。

| 変更するもの | 起点 |
|---|---|
| docs / specs / CLAUDE.md / `.claude/` ループ機構 / 公開アプリコード / infra / ops | `public-dev` |
| `ops/internal-only-paths.txt` に列挙されたもの（デモプラットフォーム等） | `internal-dev` |

判定は `ops/check-branch-base.sh` が **CI（`pull_request`）で**検査する。`internal-dev` 宛の PR が共有物しか変更していないと落ちる。内部固有パスの一覧は `ops/internal-only-paths.txt`（単一の真実源）。

**いまどちらの版を触っているかは `make where`。** 版を言い忘れたまま作業が進むのを防ぐための入口で、ブランチ・推定した起点・変更内容との整合・配備先コンパートメント・ADB の状態を1画面で出す。

起点は **「分岐点が public-dev に含まれるか」**で推定する（`mbi = merge-base(HEAD, internal-dev)` が `public-dev` の祖先なら `public-dev` 起点、そうでなければ `internal-dev` 起点）。単純な merge-base 等値比較にすると、internal-dev が public-dev に追いついていない間（同期は人間ゲートなので普通にある）に Public の作業を Internal と誤判定する。ローカルの `make lint` も同じ推定を使う（以前は base を知る術が無いとして**黙ってスキップ**しており、「ローカルでは何も言われず PR で初めて落ちる」状態だった）。**推定では落とさない** —— 明示された base ではないので `make lint` を止める根拠にはせず、WARN に留める。強制力は CI 側のまま。明示したいときは `BRANCH_BASE=internal-dev make lint`。

推定の限界: 枝を切った位置が `public-dev` にも存在するコミットなら、`internal-dev` から切っていても区別できない。実害は小さい（その位置は `public-dev` にもあるので、共有物を入れる先として `public-dev` は正しい）。

混在（内部固有＋共有物）は落とさず WARN に留める —— 分割を強制すると実務が回らないため。ただし共有部分が Public に届かないことは表示する。

一覧に足すかどうか迷ったら、**「Public 版に出しても差し支えないか」**を先に考える。差し支えないなら一覧に足すのではなく `public-dev` 起点で作り直すのが正しい対応。

> 実害の記録: 2026-07 に `docs/verification/` を4サブディレクトリへ整理した変更を `dev` 起点で作り、`main` には105ファイルが直下に残った。後追いで main 側 PR を足す羽目になり、以後 sync のたびに手で衝突を解いていた。

## 変更の流れ

### Public または両版へ出す変更（＝ほとんどの変更）

```text
public-dev → feature/* → PR → public-dev → sync PR → internal-dev
                                    ↓
                           （リリース時）release PR → main
```

1. 最新の `public-dev` から短命 feature branch を作る。
2. 受け入れ条件と ORM 検証を満たして `public-dev` へ PR merge する。
3. `ops/sync-public-to-internal.sh` で `internal-dev` へ forward merge する PR を作る。
4. Conflict は sync PR 上で解決し、Public の実装を基準に Internal 固有差分を保持する。

Public でしか訴求しない機能もこの流れにする。Internal 版に表示されても問題ないという前提なので、コードを二重管理しない。

### Internal 固有・先行機能

```text
internal-dev → feature/* → PR → internal-dev
```

`public-dev` へは merge しない。後から Public 化する場合、`internal-dev` 全体を merge せず、対象変更だけを最新 `public-dev` 上へ **cherry-pick で移植**する。Public 向けの設定・ドキュメント・互換性を確認して `public-dev` へ入れた後、通常どおり sync する。

### Public の緊急修正

```text
main → hotfix/* → main → public-dev → internal-dev
```

Deploy ボタンが参照する `main` を直し、同日中に `public-dev` へ戻して `internal-dev` まで流す。`public-dev` だけに先行適用しない。

### Internal の緊急修正

```text
internal-stable → hotfix/* → internal-stable → internal-dev
```

施主ホストの本番に効かせる修正は `internal-stable` を直し、直後に `internal-dev` へ forward merge して次期版に取り込む。

## Merge の禁止事項

- **`internal-dev` を `public-dev`（や `main`）へ merge しない。** merge はブランチ先端を丸ごと運ぶため、Internal 固有機能が Public release に入る。「この変更だけ merge」は git に無い。後から公開するなら cherry-pick。
- `internal-stable` も同様に Public 側へ merge しない。
- `main` / `internal-stable` へ feature branch を直接向けない。release PR と hotfix のみ。
- 同じ修正を Public 側と Internal 側で別々に実装しない。将来の conflict と挙動差になる。
- Public ORM の変更を `public-dev` 未反映のまま `internal-dev` だけで完了扱いにしない。
- sync PR に新機能を混ぜない。Conflict 解決だけに限定する。

## Release 手順

### Public

1. `public-dev` の CI、Terraform `infra/orm` と生成済み Deploy ZIP の validate、必要な OCI 実機確認を完了する。
2. `public-dev → main` の release PR を merge する。release workflow 成功後、Deploy ボタンが参照する `orm-main` の専用 ZIP と `:latest` イメージが更新される。
3. 公開リリース点に annotated tag `public-vX.Y.Z` を付け、release note を作る。

`main` への merge が即座に公開配信物を差し替えるため、**`main` へ入れてよいのはリリースすると決めたものだけ**。

### Internal

1. `internal-dev` の CI と Internal 環境の E2E を完了する。
2. `internal-dev → internal-stable` の release PR を merge する（リリース点のスナップショット）。
3. `internal-stable` 上のリリース点に annotated tag `internal-vX.Y.Z` を付け、release note を作る。
4. Internal release に含まれる Public 未収録機能を release note に明記する。
5. 施主ホストの本番を `internal-stable` の新タグへ更新する。

同じ commit に Public / Internal 両方の tag が付いてもよい。版ごとに version を独立して進める。

## DB migration の番号

Public と Internal が並行して番号を消費するため、帯を分ける。

| 版 | 番号帯 |
|---|---|
| Public（`public-dev` 起点） | `0xx_` |
| Internal 固有（`internal-dev` 起点） | `5xx_` |

`packages/api/jetuse_core/migrate.py` はファイル名（`f.stem`）単位で適用済みを記録するので、番号が重複しても取り違えは起きない。ただし順序の保証が失われるため新規分から帯を分ける。**既存の重複（`017`〜`021`）はリネームしない** —— `schema_migrations` に記録済みの version と食い違い、適用済みの DDL が再実行されるため。

## Branch protection

- 4ブランチとも direct push を禁止し、PR と CI 成功を必須にする。
- **bot の例外は設けない**（ADR-0030）。個人所有リポジトリでは ruleset の `bypass_actors` に GitHub Actions を指定できない（`Actor GitHub Actions integration must be part of the ruleset source or owner organization`）ため、release workflow が `main` へ dist を直接 commit する設計をやめた。`packages/web/dist` は追跡対象のまま**人がコミットする**成果物とし、陳腐化は `ci.yml` の web ジョブが検出する。
- **approval は 0 件必須**にする。単独メンテナのため自分の PR を自分で approve できず、1件以上にすると全 PR が止まる。PR 経由の強制と CI 必須で担保する（residual）。
- **review は ruleset では必須にできない**（approval 0 のため）。`main` の Public release、`internal-stable` の Internal release、および ORM / IAM 変更は、**PR 本文と Codex レビュー（`.claude/skills/codex-review`）で担保する**。CODEOWNERS も approval を要求できないので設定しない。複数人体制になったら approval 必須へ引き上げる。
- sync PR は `refactor/sync-public-internal` のように識別できる名前にする（`refactor/*` は `deploy-dev.yml` のトリガ外なので自動配備が走らない）。

## 関連

- ADR-0028: 4ブランチ体制と起点判定（本ガイドの根拠。ADR-0014 / ADR-0016 の一部を supersede）
- `ops/sync-public-to-internal.sh` / `ops/check-branch-base.sh` / `ops/internal-only-paths.txt`

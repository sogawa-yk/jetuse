# Public / Internal のブランチとリリース運用

JetUse は Public 版と Internal 版をどちらも正式なリリースとして扱う。Internal 版のコードが公開されても問題はないため、機密保持ではなく「安定した配布元」と「先行機能を含む統合先」の分離を目的にする。

## 長期ブランチ

| ブランチ | 役割 | OCI 配布 | リリース |
|---|---|---|---|
| `main` | Public 正式版。常に Deploy to Oracle Cloud 可能 | `orm-main`リリースの専用ZIP | `public-vX.Y.Z` |
| `dev` | Internal 次期版の統合（開発）。Public の全機能を含めてよい | 配信しない | – |
| `internal-stable` | Internal 安定版。施主ホストの本番が追う配信元（ADR-0016） | Internal の手順・環境 | `internal-vX.Y.Z` |

原則は `main ⊆ dev`。Public に入った変更を `dev` にも取り込み、Internal 固有機能は `dev` にだけ存在する。`internal-stable` は `dev` のリリース点のスナップショットであり、`dev` の未リリース作業に本番が引きずられないための分離（Public は各利用者が tag からセルフホストするため `main` 自体が安定版で足りる。安定枝は Internal 側のみ）。

## 変更の流れ

### Public または両版へ出す変更

```text
main → feature/public-* → PR → main → sync/main-to-dev PR → dev
```

1. 最新の `main` から短命 feature branch を作る。
2. Public の受け入れ条件と ORM 検証を満たして `main` へ PR merge する。
3. 同じ変更を個別に作り直さず、直後に `main` を `dev` へ forward merge する PR を作る。
4. Conflict は sync PR 上で解決し、Public の実装を基準に Internal 固有差分を保持する。

Public でしか訴求しない機能もこの流れにする。Internal 版に表示されても問題ないという前提なので、コードを二重管理しない。

### Internal 固有・先行機能

```text
dev → feature/internal-* → PR → dev
```

`main` へは merge しない。後から Public 化する場合、`dev` 全体を `main` へ merge せず、対象変更だけを最新 `main` 上の Public feature branch へ移植する。Public 向けの設定・ドキュメント・互換性を確認して `main` へ入れた後、通常どおり `main → dev` で同期する。

### Public の緊急修正

```text
main → hotfix/* → main → dev
```

修正を `dev` だけに先行適用しない。Deploy ボタンが参照する `main` を直し、同日中に `dev` へ同期する。

### Internal の緊急修正

```text
internal-stable → hotfix/* → internal-stable → dev
```

施主ホストの本番に効かせる修正は `internal-stable` を直し、直後に `dev` へ forward merge して次期版に取り込む。`dev` だけに先行適用しない（本番に届かないため）。

## Merge の禁止事項

- `dev` を丸ごと `main` へ merge しない。Internal 固有機能が意図せず Public release に入るため。`internal-stable` も同様に `main` へ merge しない。
- `internal-stable` へは `dev` からの release PR と hotfix 以外を入れない。feature branch を直接向けない。
- 同じ修正を `main` と `dev` で別々に実装しない。将来の conflict と挙動差になるため。
- Public ORM の変更を `main` 未反映のまま `dev` だけで完了扱いにしない。
- `main → dev` の sync PR に新機能を混ぜない。Conflict 解決だけに限定する。

## Release 手順

### Public

1. feature PRのCI、Terraform `infra/orm`と生成済みDeploy ZIPのvalidate、必要なOCI実機確認を完了する。
2. `main` へ mergeする。release workflow成功後、Deployボタンが参照する`orm-main`の専用ZIPが更新される。
3. 公開リリース点に annotated tag `public-vX.Y.Z` を付け、release note を作る。
4. `main → dev` sync PR を merge する。

### Internal

1. `dev` の CI と Internal 環境の E2E を完了する。
2. `dev → internal-stable` の release PR を merge する（リリース点のスナップショット）。
3. `internal-stable` 上のリリース点に annotated tag `internal-vX.Y.Z` を付け、release note を作る。
4. Internal release に含まれる Public 未収録機能を release note に明記する。
5. 施主ホストの本番を `internal-stable` の新タグへ更新する。

同じ commit に Public / Internal 両方の tag が付いてもよい。版ごとに version を独立して進める。

## Branch protection 推奨

- `main`・`dev`・`internal-stable` は direct push を禁止し、PR と CI 成功を必須にする。
- `main` は Public release owner、`dev` と `internal-stable` は Internal release owner の review を必須にする。
- `main` の ORM / IAM 変更には `infra/orm*` と `infra/terraform/modules/iam` の CODEOWNERS review を設定する。
- sync PR は `sync/main-to-dev` のように識別できる名前にする。

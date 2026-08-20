# ADR-0031: コンパートメント5面構成と、開発環境の Terraform 分割

- Status: **Accepted / 実施済み**（下記の承認記録を参照）
- Date: 2026-08-07（起案）/ 2026-08-09（実施完了）

## 承認記録

**人間ゲートに当たる操作は、すべて実施前に施主の指示・承認を得ている。** 会話上のやり取りが
根拠であり、リポジトリ側に痕跡が無かったため、後から辿れるようここに残す（Codex review-4 の指摘）。

| 日付 | 承認された操作 | 施主の指示（要旨） |
|---|---|---|
| 2026-08-07 | コンパートメント5面構成の採用 | 「今後のコンパートメント情報です。今後常にこのルールに従えるようにしてください」 |
| 2026-08-07 | **ホスティング環境の全撤去** | 「ホスティング環境は何もデプロイせずに削除してください…一旦綺麗にします」 |
| 2026-08-07 | 開発環境への配備 | 「まずは開発環境にそれぞれデプロイし」 |
| 2026-08-08 | 撤去範囲＝destroy job ＋ スタック外の残りも全部 | 選択肢提示に対し「destroy job → 残りも全部消す」 |
| 2026-08-08 | `public-dev` へ `environments/dev` 相当をフル構築 | 選択肢提示に対し「environments/dev を丸ごと」 |
| 2026-08-08 | 共有基盤の ORM 化（**internal-dev の移行を含む**） | 「新環境もOCI RMであげた方が良くないですか？」→ 範囲は「internal-dev も同時に移行」 |
| 2026-08-09 | 動的グループの整理（`jetuse-deploy-test-dg` 削除・専用 DG 作成・IAM ポリシー是正） | 選択肢提示に対し「２が可能であれば2で、無理だったら３」 |

IAM の CREATE/UPDATE/DELETE は施主自身が実行した（エージェント側は自動承認ゲートで停止するため、
コマンドを提示して手元で実行してもらう形を取った）。
- Amends: ADR-0028（4ブランチ体制。「コンパートメントをブランチに対応させない」という
  当時の設計原則を**取り消す**）

## Context

### コンパートメントとブランチを対応させないと決めていた

2026-08-04 の合意では、コンパートメントは3つ（`dev` / `test` / `registry`）で、
**ブランチの軸（公開範囲 × 安定度）とコンパートメントの軸（権限境界 × 壊してよさ）は
直交するから対応させない**としていた。

この前提が実態と合っていなかった。`jetuse:public` には**公開版の配備済みスタックが丸ごと
存在し**（ORM スタック `jetuse-pub` が 189 資源を管理）、`jetuse:internal-dev` には
内部版の開発環境が動いていた。つまり現場では既に「公開/内部 × 開発/配信」で使い分けていて、
**原則のほうが後追いで嘘になっていた**。

### 名前が実態を裏切っていた

`jetuse:public` は ER-0016 の記述では「旧 `jetuse:public` ＝ 現 `jetuse:registry`」とされて
いたが、**両方が実在した**。`public` は配布物置き場ではなく、公開版のホスティング環境だった。
`environments/dev` の Terraform も、名前は `dev` だが実際に指しているのは `jetuse:internal-dev`。

## Decision

### 1. コンパートメントは5面。ブランチと対応させる

| コンパートメント | 役割 | 対応するブランチ |
|---|---|---|
| `jetuse:public-dev` | パブリック版 開発環境 | `public-dev` |
| `jetuse:public` | パブリック版 ホスティング環境 | `main` |
| `jetuse:internal-dev` | インターナル版 開発環境 | `internal-dev` |
| `jetuse:internal` | インターナル版 ホスティング環境 | `internal-stable` |
| `jetuse:registry` | 配布コンテナイメージの置き場（**環境ではない**） | — |

ADR-0028 の「対応させない」原則は取り消す。2軸（公開範囲・安定度）が両者で同じ意味を持つ以上、
別の覚え方を強いる理由がない。`jetuse:test` / `jetuse:demo` は作らない。

### 2. ホスティング環境は一度空にする

`jetuse:public` の配備済みスタックを撤去し、`jetuse:internal` と揃えて空の状態から始める
（`jetuse:internal` は元から空）。ホスティングへの配備は、**リリース手順の一部として**
改めて定義する（本 ADR の範囲外・open item）。

撤去は ORM スタックの destroy job を入口にする。189 資源が state にあるため、個別削除より
依存順が正しく、取りこぼしも少ない。スタック外に残るもの（Identity Domain `jetuse-pub-domain`
2つ・User `demo`・Policy `jetuse-pub-runtime-policy`・旧 Osaka スタックの殻）は destroy 後に手で消す。

### 3. 開発環境も ORM スタックで管理する

`environments/dev` の構成を **OCI Resource Manager のスタック**として持つ。
`internal-dev` / `public-dev` の2本を、同じ `.tf` に**変数だけ変えて**当てる。

**なぜローカル CLI をやめるのか**: 共有基盤 35 資源の state が**この 1 台の 1 ファイル**にしか無く、
gitignore 済みでリモート退避も無い。失えば資源は Terraform から管理できない孤児になる。
インスタンス `dev` への無人委譲（`dispatch-remote`）から infra を触れないのも同じ理由。
ORM なら state を OCI が持ち、ジョブ履歴が残り、同時実行も直列化される。

`terraform.tfvars` が不要になるので、当初案（ディレクトリを分けて state と tfvars を隔離する）は
撤回した。変数はスタック側に持つため、そもそも取り違えようがない。

**既存の internal-dev は作り直さない。** ORM には state を持ち込む API が無い
（`get-stack-tf-state` は読むだけ）ので、Terraform 1.5 の `import` ブロックで初回 apply 時に
既存 OCID を引き取る。生成は `ops/orm-import-blocks.py`、実行は `ops/orm-stack.sh <env> import`。
**出力はコミットしない**（実 OCID を含む）。

移行 plan の受け入れ条件は **`0 to destroy`**。2026-08-07 の dry run（空 state に対する plan）で
`28 to import, 4 to add, 2 to change, 0 to destroy` を確認済み。

| 内訳 | 理由 |
|---|---|
| 4 to add | `terraform_data.empty_buckets` ×3（作成は no-op）＋ SPA の PAR ×1 |
| 2 to change | ADB の `admin_password`（API が返さない値の再設定）＋ API GW deployment のルート（新 PAR を指す） |

dry run で潰した罠が3つある。**どれも本番でやっていたら壊れていた。**

| 罠 | 何が起きるか | 対処 |
|---|---|---|
| NSG ルールとログの `id` をそのまま import ID にした | `can not marshal ... nil pointer` で plan が止まる。NSG ルールの `id` は親 NSG 内でのみ一意な短ハッシュ | 複合 ID（`networkSecurityGroups/{nsg}/securityRules/{id}` 等）を組み立てる |
| `time_offset` を作り直させた | 基準時刻が変わり **PAR が replace = SPA の URL が変わる**（`1 to destroy`） | `spa_par_expiry` を新設し、現行の失効日時を明示して `count=0` にする |
| PAR を import した | `access_uri` は**作成時にしか返らない**ため null になり、api-gateway の文字列補間が落ちる | PAR は import せず作り直す。**公開 URL は API Gateway 側なので変わらない**。旧 PAR は移行後に手で消す |

`environments/app`（開発者ごとのアプリ層）は `terraform_remote_state` の local backend で
共有 state を読んでいる。ORM の state は Terraform の backend ではないので、
`ops/dev-env-up.sh` が先に `ops/orm-stack.sh <env> state` で落とし、
`-var shared_state_path=<落とした先>` を渡す。`shared_state_path` は既に変数なので変更は数行。

秘密の扱いは `environments/dev/schema.yaml` を新設し、`adb_admin_password` と
`registry_password` を `type: password` にした（宣言が無いと ORM のコンソールで平文表示される。
Terraform 側の `sensitive` はコンソールのマスクには効かない）。

**`api_environment` は残課題。** 任意のキーを持つ map で、値に秘密（ADB のパスワード等）が
入りうるが、ORM の schema は map の一部だけをマスクできない。いまは開発環境なので許容し、
ホスティング環境の配備手順を決めるときに **Vault Secret の OCID 参照へ寄せる**か、
秘密だけ独立変数へ切り出す。open item に載せた。

### 4. ワークロードの身分はコンパートメントを越えない

動的グループ（workload principal の入口）とポリシー（出口）の**両方**をコンパートメント単位で閉じる。

| | 是正前 | 是正後 |
|---|---|---|
| `jetuse-internal-dg` の matching rule | 5面すべてを拾う | `jetuse:internal-dev` のみ |
| `jetuse-dev` ポリシーの dynamic-group 文 | internal-dev / registry / public / public-dev へ `manage all-resources` | `internal-dev` のみ（＋ tenancy の `read objectstorage-namespaces`） |
| public-dev のランタイムポリシー | — | `jetuse-pubdev-dg`（public-dev だけを拾う）へ 20 文 |

**片側だけでは閉じない。** public-dev のポリシーを `jetuse-pubdev-dg` に向けても、テナンシ直下の
blanket 文が生きている限り、どの環境の Container Instance からでも他環境を全操作できた。
入口を絞らなければ、将来また blanket 文が足されたときに同じ穴が開く。

人間のグループ（`group jetuse-dev`）の権限は変えていない。狭めたのはワークロードの身分だけ。

**動的グループは環境ごとに1本。** テナンシの DynamicResourceGroups には上限があり
（2026-08-08 時点で 50 本・うち 48 本は他プロジェクト）、環境ごとに 3 本ずつは作れない。
`enable_dynamic_group=false` にすると runtime / adb / semantic_store の3参照が
`existing_dynamic_group` に畳まれるので、1本で機能は足りる。用済みの `jetuse-deploy-test-dg`
（`jetuse:public` 専用・その環境は空にした）を削除して枠を作った。ER-0016 はこれで解消。

## Consequences

- **コストが増える。** `public-dev` に ADB と OpenSearch がもう1組建つ。使わない期間は
  `enable_adb=false` / `enable_opensearch=false` で落とせるようにしておく。
- **`jetuse:public` のデータは消える。** `jetuse-pub-adb` の中身を含む。ホスティング環境を
  一度空にする方針の帰結であり、意図どおり。
- **課金の出血が止まる。** 撤去時点で `GenerativeAiHostedDeployment` 6つと
  Container Instance 1つが ACTIVE だった。
- **ワークロードの越境が塞がった。** 是正前はどの環境の Container Instance / Function / ADB からでも
  4コンパートメントを `manage all-resources` できた。
- **名前の不整合が残る。** `environments/dev` が指すのは `jetuse:internal-dev`。
  リポジトリ内に 35 箇所の参照（`README` / `CLAUDE.md` / `ops/*.sh` / `infra/orm` / `specs`）が
  あるため、リネームは別タスクにする。

## Open items

- `environments/dev` → `environments/internal-dev` のリネーム（参照 35 箇所）
- ホスティング環境への配備手順の定義（`main` / `internal-stable` から何をどう配るか）
- `api_environment`（map）に秘密が入りうる。ORM schema でマスクできないので、Vault Secret の
  OCID 参照へ寄せるか、秘密だけ独立変数へ切り出す
- **環境間の拒否を実測していない。** 定義（DG の matching rule とポリシー）では閉じているが、
  片方の resource principal から他方を叩いて 403 になることは確かめていない
- `.env` の `COMPARTMENT_OCID` は `jetuse:internal-dev` のまま。public 側の作業をするときの
  切り替え方法を決める（プロファイル分けか、`.env.public-dev` か）
- ~~ER-0016（`jetuse:registry` に不要な権限が向いている）~~ → Decision 4 で解消
- ホスティング環境（`jetuse:public` / `jetuse:internal`）用の動的グループは未定。配備手順を
  決めるときに、同じ「環境ごとに1本・その環境だけを拾う」原則で作る

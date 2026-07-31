# PUBLIC-IAM-02 検証レポート — 専用コンパートメント権限だけでの公開デプロイ

- 実施日: 2026-07-30
- 環境: DEPLOYTEST テナンシ（ホーム `ca-toronto-1`）/ 配備先 `us-chicago-1` / 専用コンパートメント `jetuse-restricted`（新規）
- 実行者: **テナンシ権限を一切持たない検証ユーザー**（`jetuse-deployer`）
- 配布物: `orm-main` リリースの `jetuse-orm.zip`（= READMEのデプロイボタンが指す実物。`image_tag` は main の 96e7311 に固定）
- 証跡: `runs/2026-07-30T0755_PUBLIC-IAM-02/e2e/`（URL / OCID / パスワードはマスク済み）
- 前段: FIX-58（テナンシ管理者経路の成立・`PUBLIC-DEPLOY-E2E.md`）/ PORT-03（ホスト型エージェント）

## 結論

**専用コンパートメントの `manage all-resources` だけで、認証（Identity Domain・demo ユーザー）と
ホスト型エージェント3SDKを含めて、機能を削らずに配備できる**（TTS を除く。下記）。
テナンシ管理者に依頼する必要があるのは、実測の結果**次の2つだけ**だった。

```text
1. Dynamic Group を1本作る（root にしか作れないため）
2. デプロイ担当グループへ次の2文を与える
   Allow group <deployer-group> to inspect tenancies in tenancy
   Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

（コンソールでコンパートメントを選ばせるなら `inspect compartments in tenancy` を足す。
`manage orm-stacks` / `manage orm-jobs` は `manage all-resources` に含まれる。）

従来の案内にあった **Dynamic Group 向けの `read objectstorage-namespaces in tenancy` は不要**、
**Runtime Policy の事前作成も不要**（スタックがコンパートメント内に作れる）、
**`enable_auth=false` へ落とす必要もない**。

apply は **181リソース・17分・エラー0**。受け入れE2Eは **35/39 PASS**、エージェントE2Eは **9/9 PASS**。
残った4件はすべて TTS で、**同じ呼び出しがテナンシ管理者プリンシパルでも失敗する**（後述）。
したがって TTS が動くことは本検証では示せていない（`runs/.../e2e/SKIPPED.md` §1）。

なお、**必須と確認できたのは plan と destroy について**である。apply は上記2文を含む6文を
付与した状態で実施しており、apply 固有の追加要求がないことは確認していない。

既存の案内ページは「Identity Domain 管理権限が無ければ `enable_auth=false`（＝認証・管理画面・
エージェントを諦める）」と案内していたが、**この制約は実在しなかった**。本検証で書き換えた。

## 検証の設計

「デプロイできた」ではなく「**どのポリシーが要るのか**」を確定させるため、次の2段で測った。

1. **十分性**: 事前作成を「原理的にコンパートメント権限では作れないもの」だけに絞って apply → E2E → destroy。
2. **必要性**: 事前作成した各文を1つずつ外して、**何が壊れるか**を実測（壊れないなら要件から落とす）。

## Phase A: 制限環境（テナンシ管理者として用意）

詳細と否定検証は `runs/.../e2e/phase-a-setup.md`。

- 専用コンパートメント `jetuse-restricted`、グループ `jetuse-deployers`、ユーザー `jetuse-deployer`（APIキーのみ）
- Dynamic Group `jetuse-restricted-dg`（**単一・7 resource-type**。ホスト型エージェント3型を含む）
- root Policy 2本: DG への namespace 参照1文 / デプロイ担当への6文
- **runtime policy は意図的に事前作成しない**（スタックが作れるかを測るため）

制限ユーザーがテナンシ権限を持たないことの裏取り（すべて 404 NotAuthorizedOrNotFound）:
root への Dynamic Group 作成 / root への Policy 作成 / root 直下へのコンパートメント作成。

## Phase B: 十分性 — 制限ユーザーで apply

| 論点 | 結果 | 証跡 |
|---|---|---|
| ORM スタック作成・plan | 成功。plan は 181 to add で、**IAM は `module.iam.oci_identity_policy.runtime[0]` だけが作成対象**（Dynamic Group と root Policy は含まれない） | `phase-b-apply.md` |
| A: Identity Domain の**作成** | 成功（1分13秒） | 同上 |
| B: ドメインの**管理**（`oci_identity_domains_app` / `_user` / `_grant` / `_setting`） | 成功 | 同上 |
| B: FIX-58 の `UserPasswordChanger`（ORM 内で OCI CLI を委任トークン実行） | 成功。demo ユーザーの SCIM 属性が **`must-change: false`**、パスワード設定 07:59:31。実ブラウザでログインしアプリ描画まで到達 | 同上 |
| C: コンパートメント内 runtime policy をスタックが作成 | 成功（24文） | `runtime-policy-statements.txt` |
| D: ホスト型エージェント3SDK の配備 | 成功（各3分。OAuth アプリ + Hosted Application/Deployment） | 同上 |
| apply 全体 | **`Apply complete! Resources: 181 added, 0 changed, 0 destroyed.`** | 同上 |

**事前作成した以外のポリシーを1つも足さずに完走した**ため、Phase A の集合は十分だと確定した
（当初計画では「失敗を1件ずつ潰して確定する」段を想定していたが、追加は発生しなかった）。

## 受け入れ E2E（制限ユーザーが作った環境に対して）

`ops/e2e/public-deploy.mjs`（39項目）: **35 PASS / 4 FAIL**。`ops/e2e/agents-3sdk.mjs`（9項目）: **9 PASS**。

PASS したもの（抜粋）: demoユーザーがパスワード変更を強制されない / ログイン→アプリ表示 /
チャット5モデル / 会話メモリ（OCI Conversations）/ RAG のアップロード→索引化→**引用付き回答** /
DBチャットの SQL 生成・実行 / OCR / 議事録の音声登録 / リアルタイムSTT / 翻訳 /
**管理ダッシュボード（demo ユーザーで 200）** / 全ページ描画 / **3SDK エージェントのツール実行**。

FAIL 4件はすべて TTS 起因（`capability: tts` / `TTS: 音声合成` / `TTS: health が実合成を反映` と、
それに引きずられた `/api/health` 全体）。

### TTS は権限問題ではない（切り分け済み）

**テナンシ管理者のユーザープリンシパル**で OCI Speech の `list_voices` を直接叩いた実測:

| リージョン | 結果 |
|---|---|
| `us-chicago-1`（配備先） | **HTTP 500 InternalError**（3回連続で再現） |
| `us-phoenix-1`（フォールバック先） | 401 NotAuthenticated（テナンシ未購読） |
| `ca-toronto-1` | 404 NotAuthorizedOrNotFound（TTS 未提供・FIX-58 と同じ） |

管理者権限でも 500 になるため、**この構成の IAM 不足では説明できない**（原因が OCI 側の障害か
テナンシ固有の状態かまでは特定していない）。FIX-58（2026-07-28）は同じ `us-chicago-1` で
合成成功を確認しているので、その後に環境側で状態が変わったことになる。
JetUse 側の縮退は設計どおり動作（`/api/tts` が 503 + 理由、health が `unavailable` + ヒント、
他機能は無影響）。

### apply 直後の一過性の失敗（案内に反映）

1回目の E2E は 32/39 で、RAG と既定モデル（Responses 系）が
「GenerativeAI project を解決できません」で落ちた。**権限不足ではなく IAM 反映と project 自動作成の待ち**で、
同じ環境で数分後に再測すると `rag=ok` / `project ok(source=auto)` / 既定モデル応答 OK になった。
案内ページに「apply 直後の数分は権限が正しくても unavailable に見える」を明記した。

## 必要性の検証（各文を外して壊れ方を測る）

### 測り直した経緯（最初の測定は無効だった）

1回目は `oci iam policy update --policy-id … --statements file://… --force` で文を差し替えたつもりで
測っていたが、この CLI は **`--statements` と `--version-date` の同時指定が必須**で、
単独指定は `If updating either statements or version date, both parameters must be specified.` を
返して**何も更新しない**。スクリプトがこの標準エラーを `>/dev/null 2>&1` で捨てていたため、
「文を外しても plan が通る」という結果（4ケース）を出していた。実際にはポリシーは6文のまま
一度も変わっていない（`length(data.statements)` が 6 のままだった）。

**各ケースで「文数が意図どおりに変わったこと」をアサートしてから待つ**ように直して測り直した。
以下はすべて測り直し後の結果である。

### デプロイ担当グループ（`jetuse-deployers`）のテナンシスコープ文

コンパートメント3文（`manage orm-stacks` / `manage orm-jobs` / `manage all-resources`）は常に付与し、
テナンシスコープの文だけを入れ替えて、同一スタックに plan を実行した（IAM 反映のため各ケース 300 秒待機）。

| ケース | 付与したテナンシ文 | plan |
|---|---|---|
| NONE | なし | **FAILED** |
| TENANCIES | `inspect tenancies in tenancy` のみ | **SUCCEEDED** |
| OSNAMESPACE | `read objectstorage-namespaces in tenancy` のみ | FAILED |
| COMPARTMENTS | `inspect compartments in tenancy` のみ | FAILED |

→ **`inspect tenancies in tenancy` の1文だけが必須**。他の2文は代替にならない。

NONE の失敗内容（そのまま引用）:

```text
Error: Resource precondition failed
  on main.tf line 10, in resource "terraform_data" "region_guard":
    │ local.deploy_region_key is ""
Error: Resource precondition failed
  on main.tf line 17, in resource "terraform_data" "region_guard":
    │ local.deploy_region_key is ""
Error: Iteration over null value
  on providers.tf line 32, in provider "oci":
    │ data.oci_identity_region_subscriptions.this.region_subscriptions is null
```

**権限不足でも data source は 401/404 を返さず `null` を返す**。その結果、利用者には
「このリージョンは JetUse 未対応です」という**誤った理由**が表示される。実際の原因は権限である。
→ スタックを修正した（後述）。

### `read objectstorage-namespaces in tenancy`（Dynamic Group 向け・root の1文）

コードは `rag.py` / `minutes.py` / `db.py` で `client.get_namespace()` を**コンパートメント指定なし**で
呼ぶため、テナンシスコープの権限が要ると考えていた。実測は逆だった。

| 測定 | 結果 |
|---|---|
| root の `jetuse-restricted-tenancy-policy` を**削除**し 7 分待って RAG の一覧・アップロード | どちらも 200 |
| 同じ状態で議事録の音声登録 → ジョブの最終状態 | 登録 200・ジョブ **`completed`** |
| **この権限を一度も持たない新規プリンシパル**（`manage all-resources in compartment` 1文のみのグループに所属）で `GetNamespace` | **成功**（コンパートメント指定の有無を問わず namespace を返す） |

3つ目は「取り消しの反映が遅れているだけ」という疑いを排除するために行った。
→ **この1文は不要**。事前作成の依頼から外した。

なお `rag.py` の原本バックアップ／削除は `except Exception` で握り潰しているため、
仮にここが権限で落ちても HTTP は 200 のまま静かに劣化する。今回の判断は
議事録（握り潰さない経路）と新規プリンシパルでの直接確認に基づく。

### 事前作成が必要なものの最終形

| 依頼するもの | 作成先 | 必要性の根拠 |
|---|---|---|
| Dynamic Group 1本（7 resource-type） | root | コンパートメント権限では作成不可（実測 404） |
| `Allow group <g> to inspect tenancies in tenancy` | root | これだけで plan 成功／無いと失敗（実測） |
| `Allow group <g> to manage all-resources in compartment id <C>` | root またはコンパートメント | 本体の作成に必要 |
| （推奨）`inspect compartments in tenancy` | root | コンソールのコンパートメント選択。plan には不要 |

## スタックの修正

権限不足が「リージョン未対応」に化ける問題（上記）を直した。

- `infra/orm/locals.tf`: `region_subscriptions_readable` を追加（`try(length(...) > 0, false)`）。
  既存の `try(..., "")` が null を "" に丸めてしまい、原因が消えていた。
- `infra/orm/main.tf`: `region_guard` の**先頭**に、この値を条件とする precondition を追加し、
  不足している文（`Allow group <deployer-group> to inspect tenancies in tenancy`）を名指しする。
- `infra/orm/providers.tf`: `home` provider の生の for 式（`Iteration over null value` の発生源）を
  `local.home_region`（`try` 付き）参照に変更。判定と案内を region_guard に一本化した。
- リージョン判定の既存2件は `!local.region_subscriptions_readable ||` を前置し、
  **購読一覧が読めているときだけ**評価する（権限不足のときに「リージョン未対応」を併記しないため）。

実機確認（`runs/.../e2e/fix-verification.md`）: `inspect tenancies` を持たないデプロイ担当で plan すると、
修正後は **`Error: Resource precondition failed` が1件だけ**になり、本文が
`Allow group <deployer-group> to inspect tenancies in tenancy` を名指しする。
`Iteration over null value` と誤誘導のリージョンエラー2件は出なくなった。

`modules/iam` の `runtime_tenancy` policy（namespace の1文）は**残した**。実測では不要だが、
OCI の Policy Reference は GetNamespace の権限として `read objectstorage-namespaces` を挙げており、
テナンシやリージョンによる差・将来の仕様変更に対する保険として1文だけ残す価値がある。
**事前作成を依頼する対象からは外した**（依頼される側の作業を減らすのが本タスクの目的のため）。

## destroy（論点E）

destroy 直前にデプロイ担当グループを**確定した最小2文**（`inspect tenancies in tenancy` +
`manage all-resources in compartment id <C>`）に絞ったうえで実行し、
**`Destroy complete! Resources: 181 destroyed.`**（`runs/.../e2e/destroy-and-teardown.md`）。

FIX-58 で作り込んだ destroy 経路（バケット中身の一括削除・OIDCアプリの非アクティブ化・
Identity Domain の deactivate）は、テナンシ権限を持たない利用者でも通る。

なお **apply 自体は6文を持つ状態で実行している**（最小2文での apply 通し実行は未実施）。
最小集合の妥当性は「plan が `inspect tenancies` のみで成功」＋「destroy が最小2文で成功」で示している。

ホスト型エージェントの残存確認では、OCI の検索サービスが索引遅延で `ACTIVE` を返し続けた。
GenerativeAI API に直接問い合わせると 3 つの Application / Deployment はいずれも `DELETED` だった。
**残存確認に検索サービスだけを使わない**こと。

## 後始末

ORM スタック2本・Policy 3本・Dynamic Group・ユーザー2・グループ2・コンパートメントを削除済み。
テナンシに `jetuse` を含む Dynamic Group / ユーザーは 0 件。既存リソースには触れていない。

## 残存リスクと未実施

| # | 内容 |
|---|---|
| 1 | **TTS の実配備確認ができていない**。`us-chicago-1` の Speech が管理者権限でも 500 のため、この構成で TTS が動くことは示せていない（`/api/health` は `ok:false` のまま）。同じ `manage ai-service-speech-family` で動く STT 系（リアルタイムSTT・議事録）は PASS しているため IAM 起因の可能性は低い。JetUse 側の縮退動作は確認済み。詳細と受け入れ判定の扱いは `runs/.../e2e/SKIPPED.md` §1。 |
| 2 | **ADB ウォレット取得（`db.py::_wallet_bytes`）の権限確認は間接的**。namespace 権限の削除下でコンテナを再起動して bootstrap をやり直す試験は実施していない（新規プリンシパルでの `GetNamespace` 成功をもって代替とした）。 |
| 3 | **コンソール UI 経路は未検証**。Stack 作成・変数入力を CLI で行ったため、`inspect compartments in tenancy` が画面上で本当に要るかは実測していない（推奨として記載）。 |
| 4 | **Semantic Store（SQL Search）は対象外**。`semstore_ocid` 未設定の既定構成で検証した。 |
| 5 | 測定は DEPLOYTEST テナンシの 2026-07-30 時点の OCI 挙動に基づく。GetNamespace の認可挙動は OCI 側の仕様変更で変わりうる。 |
| 6 | **最小2文だけでの apply 通し実行は未実施**（apply は6文、destroy は最小2文で実施）。 |

## 副産物（本タスクで直した / 記録した他の問題）

- `ops/e2e/agents-3sdk.mjs` と `agents-degraded.mjs` が `public-deploy.mjs` と**同じ `results.json` に書いて
  先行の証跡を黙って上書き**していた（本検証で 39 項目の結果を実際に失った）。出力名を分離した。
- 測定の信頼性に関わる落とし穴を `runs/.../e2e/pitfalls.md` に記録した
  （`iam policy update` は `--version-date` 必須で単独指定は無効化される / ORM の
  `stack update --config-source` も黙って失敗しうる / 配布 zip は `git archive HEAD` 由来なので
  未コミット変更が入らない / 手動パッケージでは `.terraform` を除外する）。

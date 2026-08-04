# JetUse Public版デプロイガイド: 専用コンパートメント管理者

このガイドは、**テナンシ管理権限を持たず**、JetUse専用コンパートメントに対して次の権限を持つ利用者向けである。

```text
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

## 結論: この権限で、機能を削らずにデプロイできる

2026-07-30 に、テナンシ権限を一切持たないユーザーで実機検証した（`docs/verification/PUBLIC-IAM-02.md`）。
**認証（Identity Domain・demoユーザー）、管理ダッシュボード、ホスト型エージェント3SDKを含めて配備できる**。
権限を理由に機能を諦める必要はない。

受け入れ E2E は 39項目中 35項目 PASS ＋ エージェント 9/9 PASS。残る4項目は TTS で、
**同じ呼び出しがテナンシ管理者プリンシパルでも HTTP 500 になる**ことを確認している
（＝この構成の IAM 不足では説明できない。原因そのものは特定していない）。
詳細と受け入れ判定の扱いは §5 と `runs/2026-07-30T0755_PUBLIC-IAM-02/e2e/SKIPPED.md`。

テナンシ管理者へ依頼するのは次の2つだけである。

| # | 依頼するもの | なぜコンパートメント権限では足りないか |
|---|---|---|
| 1 | Dynamic Group 1本 | Dynamic Group は root（テナンシ）にしか作成できない |
| 2 | デプロイ担当グループへの Policy | テナンシスコープで必要と確認できたのは **`inspect tenancies in tenancy` の1文だけ**。plan はこの1文（＋コンパートメント権限）で成功し、destroy も同じ最小構成で 181 リソースを削除できた。**apply は6文を付与した状態で実施しており、apply 固有の追加要求は未確認**。コンソールからボタンで配備するなら、コンパートメント選択のために `inspect compartments in tenancy` も付ける（CLI 経路では不要と実測、コンソールでの要否は未実測） |

**Dynamic Group 向けの `read objectstorage-namespaces in tenancy` は不要**（実測）。
`manage all-resources in compartment` だけを持つプリンシパルでも `GetNamespace` は成功する。
既存スタック（`enable_dynamic_group=true`）はこの1文を root に作るが、無くても動く。

テナンシ管理権限を持つ場合は [テナンシ管理者向けガイド](./public-deploy-tenancy-admin.md) を使用する。

## 対象者チェック

すべてYesの場合にこのガイドを使用する。

- JetUse専用コンパートメントを割り当てられている。
- 専用コンパートメントで`manage all-resources`を持っている。
- テナンシの`manage dynamic-groups`または root の`manage policies`を持っていない。
- GitHubのDeploy to Oracle CloudボタンからResource Managerを利用する。

## 1. テナンシ管理者へ依頼する内容（コピペ用）

`<compartment_ocid>` は JetUse専用コンパートメントの OCID、`<deployer-group>` はデプロイ担当グループ
（Identity Domain 利用時は `<domain>/<group>` 形式）、`<dg-name>` は作成する Dynamic Group 名に置き換える。

### 1-1. デプロイ担当グループへの Policy（root に作成）

```text
Allow group <deployer-group> to inspect tenancies in tenancy
Allow group <deployer-group> to inspect compartments in tenancy
Allow group <deployer-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <deployer-group> to manage orm-jobs in compartment id <compartment_ocid>
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

各文の役割（1文ずつ外して plan した実測結果。`docs/verification/PUBLIC-IAM-02.md`）:

| 文 | 判定 | 根拠 |
|---|---|---|
| `inspect tenancies in tenancy` | **必須** | これ**だけ**を足すと plan 成功。無いと失敗。スタックはリージョン購読一覧からデプロイリージョンの region key とホームリージョン（Identity Domain の作成先）を解決する |
| `inspect compartments in tenancy` | 推奨 | これだけでは plan は通らない（`inspect tenancies` の代わりにならない）。コンソールでコンパートメントを選択・表示するために付ける |
| `manage all-resources in compartment id …` | 必須 | アプリ・IAM（コンパートメント内 Policy）・Identity Domain の作成 |
| `manage orm-stacks` / `manage orm-jobs` | 任意 | `manage all-resources` に含まれる。権限レビューを分かりやすくするため明記してよい |
| `read objectstorage-namespaces in tenancy` | **不要** | 付けなくても `GetNamespace` は成功する（この権限を一度も持たないプリンシパルで確認） |

### 1-2. Dynamic Group（1本・compact構成）

名前は任意（例 `jetuse-runtime-dg`）。JetUseのランタイムプリンシパルを1本にまとめる。

```text
Any {all {resource.type='computecontainerinstance', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='fnfunc', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='autonomousdatabase', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaisemanticstore', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaihostedapplication', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaihostedapplicationiam', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaihosteddeployment', resource.compartment.id='<compartment_ocid>'}}
```

| Resource Type | 用途 | 省略できる条件 |
|---|---|---|
| `computecontainerinstance` | FastAPI を実行する Container Instance | 省略不可 |
| `fnfunc` | Functions ルーター（presets / dbchat / tts） | 省略不可 |
| `autonomousdatabase` | Select AI / DBMS_CLOUD_AI が使う ADB のリソースプリンシパル | 省略不可 |
| `generativeaisemanticstore` | SQL Search 用 Semantic Store | `enable_semantic_store=false` なら省略可 |
| `generativeaihostedapplication` | ホスト型エージェント本体 | `enable_hosted_agents=false` なら3型とも省略可 |
| `generativeaihostedapplicationiam` | ホスト型エージェントの実行時プリンシパル | 同上（**これを落とすと配備はできても実行時に権限エラー**） |
| `generativeaihosteddeployment` | ホスト型エージェントの配備単位 | 同上 |

対象リソースが存在しない型を残しておいても問題ない（メンバーにならないだけ）。

### 1-3.〔不要〕Dynamic Group への root Policy

以前の版では次の1文を事前作成するよう案内していたが、**実測の結果これは不要**だった。

```text
Allow dynamic-group <dg-name> to read objectstorage-namespaces in tenancy
```

`manage all-resources in compartment` 相当の権限があるプリンシパルは、この文が無くても
`GetNamespace` を呼べる（この権限を一度も持たない新規プリンシパルで確認。加えて、この文を
削除した状態で RAG のアップロードと議事録の文字起こしジョブが最後まで成功した）。
テナンシ管理者に依頼する必要はない。

スタックが IAM を作る構成（`enable_dynamic_group=true`）では今もこの1文を root に作成するが、
権限としては保険であり、無くても機能する。

### 1-4.〔通常は不要〕Runtime Policy を管理者が事前作成する場合

**専用コンパートメント内の Policy 作成は `manage all-resources` に含まれるため、
通常はスタックに作らせればよい**（`enable_runtime_policy=true`。実機で確認済み）。

組織の方針でコンパートメント内でも Policy 作成を許さない場合だけ、管理者が次の24文を
`<prefix>-runtime-policy` として**専用コンパートメントに**作成し、`enable_runtime_policy=false` にする。
`<dg-name>` は 1-2 で作成した Dynamic Group 名。

```text
Allow dynamic-group <dg-name> to use generative-ai-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage generative-ai-vector-store in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage generative-ai-vectorstore-file in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage generative-ai-file in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage generative-ai-response in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage generative-ai-conversation in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage generative-ai-project in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to use autonomous-database-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read autonomous-database-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage objects in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read objects in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read buckets in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to manage ai-service-speech-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to use ai-service-document-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to use ai-service-language-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read tag-namespaces in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to use log-content in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to use metrics in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read secret-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read repos in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read vss-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to use database-tools-family in compartment id <compartment_ocid>
Allow dynamic-group <dg-name> to read database-family in compartment id <compartment_ocid>
Allow any-user to use functions-family in compartment id <compartment_ocid> where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = '<compartment_ocid>'}
```

この一覧は 2026-07-30 に**スタックが実際に作成した Policy から取得**したもの
（`enable_semantic_store=true` / `enable_project_autocreate=true` / `enable_hosted_agents=true` の構成）。
正本は [IAM Terraform module](../../infra/terraform/modules/iam/main.tf)。

漏らしやすい文:

- `generative-ai-response` / `generative-ai-conversation` は `generative-ai-family` に**含まれない**。
  欠けると既定チャットモデル・RAGの引用付き回答・会話メモリが**リソースプリンシパルでのみ 404** になる。
- `generative-ai-project` が無いと `PROJECT_AUTOCREATE=true` でも project を解決できず、RAG と
  Responses 系モデルが使えない（`PROJECT_OCID` を明示するなら不要）。
- `read repos` / `read vss-family` はホスト型エージェントの配備時に要る（イメージ取得と脆弱性スキャン結果の参照）。
- 最後の `any-user` 文は API Gateway → Functions の呼び出し。これが無いと presets / dbchat / tts が 500 になる。

## 2. デプロイ担当が Resource Manager で設定する変数

| 変数 | 値 | 理由 |
|---|---|---|
| `compartment_ocid` | 専用コンパートメント | — |
| `enable_dynamic_group` | `false` | Dynamic Group は事前作成（1-2） |
| `existing_dynamic_group` | 1-2 の Dynamic Group 名 | 全 statement がこの名前を参照する |
| `enable_runtime_policy` | `true` | コンパートメント内 Policy はスタックが作れる（1-4 で事前作成した場合のみ `false`） |
| `existing_iam_covers_hosted_agents` | `true` | 事前作成 DG がホスト型3型を含むことの申告。無いと plan が止まる |
| `enable_auth` | `true`（既定） | Identity Domain と demo ユーザーは専用コンパートメント権限で作成できる |
| `enable_hosted_agents` | `true`（既定） | 大阪 / シカゴのみ有効。他リージョンでは自動的に無効 |

`existing_dynamic_group` が実在の Dynamic Group 名と一致しないと、Policy 作成が
400 `"No permissions found"` でスタック全体を失敗させる。

## 3. デプロイ前チェック

- 管理者から 1-1（デプロイ担当の Policy）と 1-2（Dynamic Group）の設定完了の連絡を受けた。
- Dynamic Group / Policy 作成から 5〜10分待った。
- 配備リージョンが `ap-osaka-1` / `us-chicago-1` である（GenAI 実証済み。`ap-tokyo-1` /
  `us-ashburn-1` は `allow_unvalidated_genai_region=true` の明示オプトインが必要）。
- ADB、Container Instances、Functions、Identity Domain の service limit を確認した。

## 4. Deploy to Oracle Cloud

1. READMEの**Deploy JetUse to Oracle Cloud**ボタンを開く。
2. Stack compartment に JetUse専用コンパートメントを選択する。
3. §2 の変数を設定する。
4. Plan を実行し、`oci_identity_dynamic_group` と root の Policy が**作成対象に含まれていない**ことを確認する
   （含まれていれば `enable_dynamic_group` が `true` のまま）。
5. Apply する。実測 17分（181リソース。ADB 作成と IAM 反映待ちを含む）。

## 5. デプロイ後チェック

**apply 直後の数分間は、権限が正しくても機能が `unavailable` に見える**。IAM の反映と
GenerativeAI project の自動作成が終わっていないためで、実測では apply 完了から数分で解消した
（`/api/health` が `ok:false`、RAG が「project を解決できません」）。**5〜10分待ってから確認する**。

1. Output の `app_url` を開き、`demo_username` / `demo_password` でログインする
   （パスワード変更を要求されないこと）。
2. `/api/health` の各 capability（`chat` / `rag` / `dbchat` / `speech` / `ocr` / `agents`）が `ok`。

   **`tts` だけは配備リージョンの OCI Speech の提供状況に依存する**。TTS が使えないと
   `capabilities.tts` が `unavailable` になり、**全体の `ok` も `false` になる**（他の機能は無影響）。
   本ガイドの検証時（2026-07-30 / `us-chicago-1`）は OCI 側の障害で TTS が使えず、
   テナンシ管理者権限でも合成できなかったため、`ok: false` のまま完了とした。
   音声合成が必要な場合は、提供リージョンを `TTS_REGION` に明示してから再確認する。
   したがって受け入れ判定は「**`tts` 以外の capability がすべて `ok`**」で行い、
   TTS を使う構成でだけ `ok: true` と `/api/tts` の成功を条件に加える。
3. チャットで既定モデル（gpt-oss-120b）に応答させる。
4. RAG に小さいテキストを登録し、索引化完了後に引用付きで回答させる。
5. DBチャットで SQL 生成→実行。
6. `/admin` が開ける（既定では demo ユーザーが管理者）。
7. `/agents` で3SDKのエージェントが応答する（配備した場合）。

## 6. 権限エラーの切り分け

| 症状 | 主な原因 | 対処 |
|---|---|---|
| Stack を作れない | デプロイ担当グループの ORM 権限不足 | 1-1 を依頼 |
| plan が region 解決で失敗する | `inspect tenancies in tenancy` 不足 | 1-1 を依頼 |
| plan で `existing_iam_covers_hosted_agents` のエラーが出る | 事前作成 DG がホスト型3型を含むか未申告 | 1-2 を確認して `true` にする |
| Policy 作成が 400 `"No permissions found"` | `existing_dynamic_group` の名前が実在しない | 名前を修正 |
| apply 直後だけ RAG / 既定モデルが unavailable | IAM 反映と project 自動作成の待ち | 5〜10分待つ |
| RAG アップロード・議事録が実行時に失敗し続ける | Dynamic Group の matching rule に該当リソース型が無い（`computecontainerinstance` / `fnfunc`） | 1-2 を確認 |
| チャット・RAG・会話メモリが 404 のまま | runtime policy の agentic 系 resource-type 不足 | 1-4 の一覧と突き合わせる |
| presets / dbchat が 500 | API Gateway → Functions の `any-user` 文が無い | 1-4 の最終行を確認 |
| TTS だけ 503 | リージョンの提供状況（テナンシ権限でも失敗する） | `TTS_REGION` で提供リージョンを指定する |

問い合わせ時は Stack OCID、Job OCID、失敗した Terraform resource 名、OCI エラーコード、request ID を共有する。
Terraform state や生成パスワードは共有しない。

## 7. Destroy

デプロイ担当が Resource Manager の Destroy を実行できる（実機確認済み）。
Identity Domain の非アクティブ化と OIDC アプリの停止もスタックが行う。

`enable_dynamic_group=false` で参照した**事前作成の Dynamic Group とデプロイ担当 Policy は Destroy 対象外**
なので、JetUse をやめるときは管理者に削除を依頼する。`enable_runtime_policy=true` で作成した
コンパートメント内 Policy は Destroy で削除される。

## 8. 付与しない権限

デプロイ担当には次を付与しない。

```text
Allow group <deployer-group> to manage all-resources in tenancy
Allow group <deployer-group> to manage dynamic-groups in tenancy
Allow group <deployer-group> to manage policies in tenancy
```

## 関連資料

- [検証レポート（この構成の実機結果）](../verification/PUBLIC-IAM-02.md)
- [Public版 IAM要件](./public-iam-requirements.md)
- [Dynamic Group compact構成](./dynamic-group-matching-rules.md)
- [Resource Managerデプロイ](./orm.md)

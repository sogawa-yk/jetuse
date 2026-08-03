# 大阪(ap-osaka-1)の JetUse 配備を撤去する手順

- 対象: `ap-osaka-1` / コンパートメント `jetuse-proto`(`.env` の `COMPARTMENT_OCID`)
- 前提: **シカゴが動いてから実行する**(§2 の確認が全部通ってから §4 へ進む)
- **実行するのは人間。** ループ/エージェントはこの手順を実行してはならない
  (`terraform destroy` と削除は人間ゲート — CLAUDE.md)。
- 棚卸しの実測: 2026-08-03(`runs/2026-08-03T1125_AGT-06/e2e/inventory-osaka.json`)
- 撤去範囲は **ADR-0027 §5-B = B-1(完全撤去)** に決定済み(2026-08-03 人間ゲート)。
  ただし §1「消してはいけないもの」は **B-1 でも対象外**。
  **B-1(完全撤去)に決まってから §4 を実行すること。** B-2/B-3 なら §4 の一部だけ。

> **この手順書の要点は「消してよいもの」と「消してはいけないもの」を分けること。**
> 同じコンパートメントに、案件デモの資産とループの共有資産が同居している。

---

## 1. 消してはいけないもの(**先に読む**)

| 資産 | 何か | なぜ残すか |
|---|---|---|
| **`mnpdemo-mock-v3`**(Container Instance) | 案件デモのモック API | **移設対象外**。JetUse 本体ではない |
| **`mnpdemo-mock-v2`**(Container Instance) | 同上(旧版) | 同上。使用状況を確認せず消さない |
| **`mnpdemo-apigw`**(API Gateway) | 案件デモのゲートウェイ | 同上。**壊すとデモが死ぬ** |
| **`jetuseloop2`**(Autonomous Database) | **ループ E2E の共有 ADB** | AGT-06 以外のタスクも使う。**JetUse の配備物ではない**。消すとループ全体が止まる |
| **`jetuse-loop-project`**(GenAI project・大阪) | ループ E2E の GenAI project | 同上。`infra/terraform/environments/loop/main.tf` が前提にしている |
| VCN **`develop`** / インスタンス **`dev`** / バケット **`jetuse-oci-source-documents`** | 既存資産 | CLAUDE.md で参照のみ・変更削除禁止。**別コンパートメントにあり、この手順の対象外**(2026-08-03 に `jetuse-proto` から見えないことを確認) |

**判断に迷ったら消さない。** 名前に `mnpdemo` / `loop` が含まれるものは、この手順では触らない。

---

## 2. 実行してよい条件(全部通ってから §4 へ)

- [ ] シカゴの共有基盤(`environments/dev`)が apply 済みで、リソースが ACTIVE
- [ ] シカゴに GenerativeAiProject がある(**project はリージョン別**。
      2026-08-03 時点で `jetuse-loop-project` がシカゴにも ACTIVE で存在)
- [ ] シカゴの JetUse が起動し、**`/api/health` が正常**
- [ ] シカゴでエージェントが動く(`spikes/agt06/e2e.py` が全シナリオ通過)
- [ ] 大阪の ADB に**シカゴ側へ移していないデータが無い**ことを確認した
      (会話履歴・RAG 索引・登録済み外部 HTTP ツール。**移行するなら先に移す**)
- [x] ADR-0027 §5-B が **Accepted**。撤去範囲は **B-1(完全撤去)** に決定(2026-08-03 人間ゲート)

> **B-1 でも §1「消してはいけないもの」は対象外。** 撤去するのは **JetUse 本体の配備物**であり、
> 案件デモの `mnpdemo-*`・ループ共有の `jetuseloop2` / `jetuse-loop-project`・
> 別コンパートメントの既存資産は**残す**。
>
> **B-1 を選んだことで受け入れた代償**(ADR-0027 §5-B):
> 大阪にしか無いモデルのデモができない / 日本国内にデータを置く要件のデモに応えられない。
> 必要になったら Terraform で大阪へ配備し直す(state は別なので作り直せる)。
>
> **既定モデルが `grok-4.3`(シカゴのみ)になった**ため、撤去後に大阪へ切り戻す場合は
> `DEFAULT_MODEL` も戻す必要がある(§5 切り戻し)。

**データ移行はこの手順書の範囲外。** 消す前に要否を判断すること。

---

## 3. シカゴを立ち上げる(撤去の前)

```bash
# 1) 共有基盤(シカゴ)
cd infra/terraform/environments/dev
terraform init
terraform plan  -var region=us-chicago-1 -var tenancy_ocid=<...> -var compartment_ocid=<...>
terraform apply -var region=us-chicago-1 -var tenancy_ocid=<...> -var compartment_ocid=<...>
#   2026-08-03 の plan 実測: 33 to add, 0 to change, 0 to destroy

# 2) 開発者ごとのアプリ・スタック
#    ★ 共有基盤を apply するまで plan できない(../dev/terraform.tfstate を読むため)
cd ../app
terraform plan -var-file=<dev>.tfvars -state=<dev>.tfstate
```

`<dev>.tfvars` は `alice.tfvars.example` を写す。**`region` と `api_image_url` の
レジストリを合わせること**(シカゴは `ord.ocir.io`、大阪は `kix.ocir.io`)。
ops スクリプトは `ops/_region.sh` が自動で解決する。

ワンクリック配備(`infra/orm`)でシカゴへ出す場合の plan 実測は **169 to add, 0 to change,
0 to destroy**。region_guard はシカゴを「GenAI 実証済み」として通す。

**シカゴ配備で覚えておくこと**:
- **`TTS_REGION` をシカゴに設定しない。** 2026-08-03 時点でシカゴの TTS は 500 で失敗する。
  未設定なら自動で `us-phoenix-1` へフォールバックして動く(`docs/verification/AGT-06.md` §6.1)。
- `PROJECT_OCID` は**シカゴの** project OCID を入れる(大阪のものは 400 になる)。

---

## 4. 大阪を撤去する(**シカゴが動いてから・人間が実行**)

### 4-a. まず止める(消さずに影響を見る)

削除の前に**停止**して、誰も困らないことを数日確認するのが安全。

```bash
# Container Instance を停止(削除ではない。戻せる)
oci container-instances container-instance stop \
  --container-instance-id <jetuse-dev-app-api の OCID> --region ap-osaka-1
```

`jetuse-dev-app-api` を止めても `mnpdemo-*` は動き続ける(別インスタンス)。

### 4-b. Terraform で消す(**state のある分はこちらから**)

Terraform が作ったものは Terraform で消す。手で消すと state と実体がずれる。

```bash
cd infra/terraform/environments/app
terraform destroy -var-file=<dev>.tfvars -state=<dev>.tfstate   # 開発者ごとのスタック
cd ../dev
terraform destroy -var region=ap-osaka-1 -var ...               # 共有基盤
```

**`terraform destroy` の前に必ず `terraform plan -destroy` を読む。**
`mnpdemo-*` と `jetuseloop2` が対象に入っていないことを目視すること
(これらは Terraform 管理外なので**本来入らない**。入っていたら止めて調べる)。

### 4-c. 消す対象(2026-08-03 の実測棚卸し)

| 資産 | 種別 | 判断 |
|---|---|---|
| `jetuse-dev-app-api` | Container Instance | **消す**(JetUse 本体) |
| `jetuse-dev-app-apigw` | API Gateway | **消す** |
| `jetuse-dev-app-vcn` | VCN | **消す**(他が参照していないことを確認してから) |
| `jetuse-dev-app-vault` | Vault | **消す**(削除は予約 = 猶予期間あり。外部 HTTP ツールの秘密が入っている。**シカゴへ移してから**) |
| `jetuse-dev-app-spa` | バケット | **消す**(中身を空にしてから) |
| `jetuse-dev-app-speech` | バケット | **消す**(議事録の音声。**要否を確認してから**) |
| `jetuse-spike-sp202-rag` | バケット | **消してよい**(検証用の残骸。`jetuse-spike-` 接頭辞) |
| `jetuse-spike-sp303-e2e` | バケット | **消してよい**(同上) |
| `jetuse-spike-m1-project` ×5 | GenAI project | **すでに DELETED**。作業不要 |
| `mnpdemo-mock-v3` / `-v2` / `mnpdemo-apigw` | — | **消さない**(§1) |
| `jetuseloop2`(ADB) / `jetuse-loop-project` | — | **消さない**(§1) |

**ADR-0027 §5-B で B-2(推論だけ残す)を選んだ場合**: 上表の「消す」は全部実行してよい。
GenAI は配備物ではない(モデルはマネージド)ので、**大阪の推論を使い続けるのに
残すべきリソースは `jetuse-loop-project` だけ**。

### 4-d. 消したあと

- [ ] `mnpdemo-*` のデモが動くことを実際に叩いて確認
- [ ] ループの E2E(`jetuseloop2` / `jetuse-loop-project` 大阪)が通ることを確認
- [ ] `.env` / `<dev>.tfvars` に残った大阪の OCID を掃除
      (`PROJECT_OCID` / `SEMSTORE_OCID` / `ADB_OCID` / `APIGW_ENDPOINT`)
- [ ] 課金コンソールで大阪の課金が落ちたことを翌月確認

---

## 5. 切り戻し

シカゴで問題が出たら、`region` を `ap-osaka-1` に戻して apply し直せば再作成できる
(§4-b を実行する前なら停止の解除だけで戻る)。**ただし ADB のデータは destroy で消える。**
そのため §4-b は「止めて数日様子を見てから」が推奨(§4-a)。

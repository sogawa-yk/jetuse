# 【人間作業】IAM設定手順（統合版: SQL Search + アプリ実行基盤）

テナンシの動的グループ/ポリシー数制限を考慮し、**動的グループ1つ + ポリシー1本**に統合した版（2026-06-10）。
旧 `sql-search-iam.md`（SemanticStore enrichment用）と `app-iam.md`（INFRA-01のCI/Functions用）を置き換える。
Terraform側の定義は `infra/terraform/modules/iam/main.tf`（`enable_iam=false` で切り離し中）と同期している。

## 1. 動的グループ 1つ（テナンシ / Default Identity Domain）

名前: `jetuse-dg`（任意）

マッチングルール（3つのリソースタイプを `Any` で束ね、jetuse-protoコンパートメントに限定）:

```
Any {all {resource.type='generativeaisemanticstore', resource.compartment.id='<jetuse-protoのOCID>'},
     all {resource.type='computecontainerinstance', resource.compartment.id='<jetuse-protoのOCID>'},
     all {resource.type='fnfunc', resource.compartment.id='<jetuse-protoのOCID>'}}
```

- `generativeaisemanticstore`: SQL Search enrichment（SPIKE-04完結に必要）
- `computecontainerinstance`: FastAPI（SSE系）のリソースプリンシパル
- `fnfunc`: OCI Functions（非ストリーミングAPI）のリソースプリンシパル

## 2. ポリシー 1本（jetuse-protoコンパートメント。名前: jetuse-policy 等）

ステートメント6文（1ポリシーに複数文を入れられるため本数は1）:

```
allow dynamic-group jetuse-dg to use generative-ai-family in compartment jetuse-proto
allow dynamic-group jetuse-dg to use database-tools-family in compartment jetuse-proto
allow dynamic-group jetuse-dg to read database-family in compartment jetuse-proto
allow dynamic-group jetuse-dg to read autonomous-database-family in compartment jetuse-proto
allow dynamic-group jetuse-dg to read secret-family in compartment jetuse-proto
allow dynamic-group jetuse-dg to manage objects in compartment jetuse-proto
```

> Identity Domainが Default 以外の場合は `dynamic-group '<domain名>'/'jetuse-dg'` の表記にする。

### 統合のトレードオフ（承知の上で採用）

3種のプリンシパルが権限の和集合を持つ（例: Container InstanceにもDBTools権限が付く）。
コンパートメントスコープ内に閉じており、プロトタイプ段階では許容。Phase 8（セキュリティ強化）で最小権限への分割を再検討する。

## 代替案: 動的グループを1つも作れない場合

ポリシーの `where` 条件で `request.principal` を直接判定すれば**動的グループゼロ**にできる。
各文に以下の形の条件を付ける（ポリシー1本・6文は同じ）:

```
allow any-user to use generative-ai-family in compartment jetuse-proto
  where all {request.principal.compartment.id='<jetuse-protoのOCID>',
             any {request.principal.type='generativeaisemanticstore',
                  request.principal.type='computecontainerinstance',
                  request.principal.type='fnfunc'}}
```

`any-user` 表記になるが、where条件で対象コンパートメントのリソースプリンシパル3種に限定される。
（採用した場合はエージェントに伝えてください。Terraform定義をこちらの形に合わせます）

## 重要: 作成順序（2026-06-10実測）

**SemanticStoreは必ずIAM整備の「後」に作成すること。** IAM整備前に作成したストアは、整備後もenrichmentがFAILEDのまま（詳細空・再試行無効）になり、**ストアの作り直しでしか直らない**。

## 3. 完了後にエージェントへ伝えること

「**IAM整備完了**」の一言で以下を再開する:

1. SemanticStore enrichment再実行（FULL_BUILD, SH）→ SUCCEEDEDまでポーリング
2. `generateSqlFromNl` 日本語10問評価 → `docs/verification/SPIKE-04.md` 完成
3. CI/Functionsのリソースプリンシパル署名検証（`packages/api/jetuse_core/genai.py` のTODO）

参照: https://docs.oracle.com/en-us/iaas/Content/generative-ai/semantic-store-permissions.htm

## 追記（2026-06-11、AGT-02）: MCP認証情報のVault保存に必要な追加ステートメント

認証付きMCPサーバー登録（アプリがVault secretを作成）を有効にする場合、ポリシーに以下を追加:

```
allow dynamic-group jetuse-dg to manage secret-family in compartment jetuse-proto
```

追加されるまでアプリは認証なしMCPサーバーのみ受け付ける（501で案内）。

## 追記（2026-06-12、AGT-04）: ホスト型エージェント（Applications/Deployments）に必要な追加設定

hosted-deployment作成時、サービスがOCIRイメージをpull/スキャンできず artifact FAILED になることを実機確認済み
（エラー: "container image could not be accessed or validated"）。有効化には以下の2点を追加:

1. **動的グループ `jetuse-dg` のマッチングルールに2リソースタイプを追加**:

```
all {resource.type='generativeaihostedapplication', resource.compartment.id='<jetuse-protoのOCID>'},
all {resource.type='generativeaihosteddeployment', resource.compartment.id='<jetuse-protoのOCID>'}
```

2. **ポリシー `jetuse-policy` にステートメント追加**:

```
allow dynamic-group jetuse-dg to read repos in compartment jetuse-proto
```

> エージェントコンテナからのLLM呼び出し（リソースプリンシパル）は既存の
> `use generative-ai-family` 文でカバーされる。

**→ 2026-06-12 適用完了・E2E成功**（注意: 動的グループ変更の反映に5〜10分。ポリシー文のみでは不可で、
マッチングルールへの2タイプ追加が必須 — 実際に片方だけで3回FAILEDした）。検証詳細は docs/verification/agt-04.md。

参照: https://docs.oracle.com/en-us/iaas/Content/generative-ai/deploy-permissions.htm

## 追記（2026-06-12、VOICE-01）: 議事録機能（OCI Speech）に必要な追加ステートメント

Container Instance（リソースプリンシパル）からバッチ文字起こしジョブを作成・参照するために必要:

```
allow dynamic-group jetuse-dg to manage ai-service-speech-family in compartment jetuse-proto
allow dynamic-group jetuse-dg to read buckets in compartment jetuse-proto
allow dynamic-group jetuse-dg to read tag-namespaces in compartment jetuse-proto
allow dynamic-group jetuse-dg to inspect tag-namespaces in compartment jetuse-proto
```

> **実機検証で確定（2026-06-12）**: 1文目だけではジョブ作成は通るが処理が
> `INTERNAL_ERROR`（percent=0）でFAILEDする。公式ポリシー要件は
> `manage object-family` + tag-namespaces read/inspect
> （https://docs.oracle.com/en-us/iaas/Content/speech/using/policies.htm ）。
> 既存の `manage objects` との差分はバケットレベル権限のため、最小追加は
> `read buckets` + tag-namespaces 2文（それでも失敗する場合は
> `manage objects` を `manage object-family` に置換する）。
> 適用されるまで実環境の音声アップロードは503（「IAM未整備の可能性」メッセージ）になる。
> ローカル開発（~/.oci ユーザー認証）は影響なし（SPIKE-06/VOICE-01で実証済み）。

## 追記（2026-06-13、ARCH-02）: API Gateway→Functions呼び出しに必要なポリシー

API GWの `ORACLE_FUNCTIONS_BACKEND` ルートがfunctionをinvokeするための公式要件
（無いとGWが一律 `500 Internal Server Error` を返す — 実測で確認。function直接invokeは正常）:

```
ALLOW any-user to use functions-family in compartment jetuse-proto where ALL {request.principal.type= 'ApiGateway', request.resource.compartment.id = '<jetuse-protoコンパートメントのOCID>'}
```

> 参照: https://docs.oracle.com/en-us/iaas/Content/APIGateway/Tasks/apigatewayaddingfunctionbackend.htm
> 適用されるまで、fnルーター担当セグメント（presets / dbchat / tts）はGW経由で500になる。
> ※CI側に同エンドポイントが残っているため、ポリシー適用前にユーザー影響を避けたい場合は
>   tfvarsの `fn_router_image` を空にしてapplyすればCIルートに戻る（現状はfn優先のまま）。

## 追記（2026-06-13、OPS-02）: 可観測性のマネージドサービス書き込みに必要なポリシー

アプリ（CI/Functions、動的グループ `jetuse-dg`）が OCI Logging / Monitoring へ直接書き込むために必要:

```
allow dynamic-group jetuse-dg to use log-content in compartment jetuse-proto
allow dynamic-group jetuse-dg to use metrics in compartment jetuse-proto
```

- `use log-content`: `jetuse_core/obs.py` の PutLogs（カスタムログ ingestion）用
- `use metrics`: 同 PostMetricData（カスタムメトリクス名前空間 `jetuse_dev`）用
- **適用前の挙動**: 送信は失敗するがベストエフォートのためサービスは正常稼働（stderrに `oci log ship failed` / `oci metric ship failed` が出るのみ）。ログはCI/Fnのstdout経由でサービスログには乗る
- 適用後: アプリのJSON Linesが `jetuse-dev-app` カスタムログに、呼出数/トークン数が Monitoring `jetuse_dev` 名前空間（次元: feature/model/status）に届く

> 参照: Logging Ingestion / Monitoring の各サービスポリシー

## 追記（2026-06-13、GAP-04）: マネージド・ホスト型エージェントの常設に必要な人間作業

案1（Hosted Applicationを常設し「マネージド・エージェント」として配線）の前提。AGT-04で一度実施し
クリーンアップ済みのため再整備が必要。**IAMポリシー変更/Identity Domain設定変更のため人間作業。**

### 1. IDCS リソース+クライアント兼用アプリ（jetuse-dev-domain内）
- 名前例: `jetuse-agent`（confidential、is-o-auth-client + is-o-auth-resource）
- audience: `jetuse-agent` / allowed-scope(fqs): `jetuse-agentinvoke`（scope=`invoke`）
- grant: client_credentials。client_id/secret を控える（アプリの `HOSTED_AGENT_CLIENT_ID/SECRET` に設定）

### 2. 動的グループ `jetuse-dg` のマッチングルールに2タイプ追加（jetuse-proto限定）
```
all {resource.type='generativeaihostedapplication', resource.compartment.id='<jetuse-protoのOCID>'},
all {resource.type='generativeaihosteddeployment', resource.compartment.id='<jetuse-protoのOCID>'}
```
（※反映に5〜10分。ポリシー文だけでなくマッチングルールへの2タイプ追加が必須 — AGT-04の実ハマり）

### 3. ポリシー（既存の `read repos` が無ければ追加）
```
allow dynamic-group jetuse-dg to read repos in compartment jetuse-proto
```

> 上記適用後、エージェント側で `ops/deploy-hosted-agent.sh`（常設用にリポジトリ名
> `jetuse-dev-hosted-agent`・audience `jetuse-agent` へ調整した版）でデプロイし、
> `.env`/tfvars の `HOSTED_AGENT_*` に APP OCID と IDCS資格情報を設定 → framework=hosted が有効化。

## 追記（2026-06-16、ENH-10）: 翻訳の OCI Language 方式（任意）に必要なポリシー

リアルタイム文字起こしの逐次翻訳（`jetuse_core/translate.py`）で **backend=oci_language** を
使う場合のみ必要。既定の **backend=llm**（llama-3.3-70b）は既存 `use generative-ai-family`
でカバー済みのため、このポリシーが無くても翻訳機能は動く。

```
allow dynamic-group jetuse-dg to use ai-service-language-family in compartment jetuse-proto
```

> **実機検証で確定（2026-06-16）**: 未付与だと CI のRPからの
> `batch_language_translation` が **404 NotAuthorizedOrNotFound** で失敗する
> （ローカルのユーザー認証では成功するため SPIKE-E5 では露見しなかった）。
> 現在は `translate()` が ServiceError を捕捉して **自動的にLLM方式へフォールバック**
> するため、ユーザーには翻訳が壊れて見えない（CIログに `falling back to llm` 警告のみ）。
> 上記適用後は OCI Language 方式（翻訳特化・最速）が選択時にそのまま機能する。
> 参照: https://docs.oracle.com/en-us/iaas/Content/language/using/policies.htm

## 追記（2026-06-16、ENH-07）: OCR（OCI Document Understanding）に必要なポリシー

OCR画面（`jetuse_core/docunderstand.py` / `POST /api/ocr`）が CI のRPから `analyze_document`
（同期OCR）を呼ぶために必要。

```
allow dynamic-group jetuse-dg to use ai-service-document-family in compartment jetuse-proto
```

> **SPIKE-E4で確定（2026-06-16）**: ローカル（ユーザー認証）では成功するが、未付与のCI（RP）では
> `analyze_document` が **404 NotAuthorizedOrNotFound**（翻訳ENH-10と同型）。
> `docunderstand.ocr()` は 401/404 を「IAM未整備の可能性」の422へ変換するため、
> 未付与でもアプリは500を出さず安全に失敗する。付与後はOCRがそのまま機能。
> 参照: https://docs.oracle.com/en-us/iaas/Content/document-understanding/using/about_doc_understanding.htm

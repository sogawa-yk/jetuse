# PORT-03 検証レポート — 公開スタックからのホスト型エージェント配備

- 実施日: 2026-07-29
- 環境: OCI CLI プロファイル `DEPLOYTEST` / コンパートメント `jetuse-test` / デプロイ先 `us-chicago-1`（ホームは `ca-toronto-1`）
- 方法: 配布 zip（`scripts/package-orm-stacks.sh`）をローカル生成 → Resource Manager スタック作成 → apply → 実ブラウザ E2E → destroy
- 証跡: `runs/2026-07-29T1210_PORT-03/e2e/`（URL / OCID / パスワードはマスク済み）

## 結論

公開スタックからのデプロイで、**3SDK のホスト型エージェントが手動作業ゼロで使える**ことを実機で確認した。
ただし当初の方式（Terraform provider のリソース）は**上流バグで成立せず**、OCI CLI 経由の作成へ切り替えている（後述）。

## 完了条件に対する結果

| # | 完了条件 | 結果 |
|---|---|---|
| 1 | クリーンなテナンシへ既定のまま手動作業ゼロで apply が成功する | ✅ 成功（下記「apply の経緯」の注記あり） |
| 2 | `/agents` で 3SDK を切り替えてツール実行を伴う応答が返る | ✅ **9/9 PASS**（3SDK × 作成 / 未設定エラー無し / ツール実行） |
| 3 | `capabilities.agents` が `ok`、無効化構成では理由付き `unavailable` | ✅ 双方確認（`ok` / `unavailable` + ヒント） |
| 3b | `min_replica=0` からのコールドスタートが timeout(180秒) に収まる | ✅ 35分アイドル後の初回で 5.5〜6.4 秒 |
| 4 | destroy が成功し、ホスト型リソースが残らない | ✅ エージェント無効化時点で 3 本とも削除済みを確認。full destroy も完了 |
| 5 | `make lint && make test` / `terraform validate` / `terraform test` が緑 | ✅ pytest 360 passed / ruff clean / validate・fmt OK / tftest（iam 7・hosted-agent 2） |
| 6 | 本レポート | ✅ 本ファイル |

## E2E シナリオと結果

| # | シナリオ | 結果 | 証跡 |
|---|---|---|---|
| 1 | 既存項目の非回帰 | **39/39 PASS・4xx/5xx 0件** | `scenario-1-*` |
| 2 | 3SDK のエージェント実行 | **9/9 PASS** | `scenario-2-3sdk-results.json` |
| 3 | コールドスタート実測 | ウォーム 1.8〜2.3 s / 35分アイドル後 5.5〜6.4 s | `scenario-3-*` |
| 4 | 無効化構成の縮退 | **6/6 PASS** | `scenario-4-degraded-results.json` |
| 5 | destroy | 成功・ホスト型リソース残存なし | `scenario-5-destroy.md` |

シナリオ1は従来の 38 項目に、本タスクで追加した `capability: agents` が加わって 39 項目になった。
`capabilities.agents` の実値:

```json
{"status":"ok","sdks":{"openai_agents":{"ok":true},"langgraph":{"ok":true},"adk":{"ok":true}}}
```

無効化構成での実値（内部識別子ではなく対処が読める文言になっている）:

```
このスタックにはホスト型エージェントが配備されていません。スタック変数 enable_hosted_agents と、
デプロイ先リージョン(配備対象は 大阪 ap-osaka-1 / シカゴ us-chicago-1)をご確認ください
```

## 実機で判明した重要な事実

### 1. `oci_generative_ai_hosted_application` は provider 8.24.0 で使えない（方式変更の理由）

Hosted Application の作成・削除は**サービス側では成功**する（work request は `SUCCEEDED`・errors 空、
リソースは `ACTIVE`）のに、Terraform は毎回失敗する:

```
Error: Work Request error
Provider version: 8.24.0
Error Message: work request did not succeed, workId: ..., entity: hostedapplication, action: CREATED
```

原因は provider の照合ロジック。`hostedApplicationWaitForWorkRequest` は work request の resources を
`strings.Contains(strings.ToLower(res.EntityType), "hostedapplication")` で探すが、サービスが返す
`entityType` は **`HOSTED_APPLICATION`**（アンダースコア入り）で、小文字化しても `hosted_application` に
しかならず一致しない。結果 `identifier` が nil のまま失敗扱いになる。

**収束しないのが厄介**: create 失敗でもリソースは state に記録されるが tainted になり、次の apply は
「削除 → 再作成」を試みて delete 側でも同じ誤判定で落ちる。8.24.0 が最新で修正版は無い（2026-07-29 時点）。

→ **作成・削除は `oci raw-request`（`terraform_data` + local-exec）、OCID の参照だけ data source
（`oci_generative_ai_hosted_applications`）** に切り替えた。データソースは list/get のみで work request を
待たないため影響を受けない。

### 2. 環境変数はスカラーで渡すと引用符が値に残る

provider は `environment_variables.value` を「JSON として妥当な文字列」しか受け付けないが、
中身を `json.Unmarshal` せずそのまま送る。API も verbatim 保存する（実測）。

| 渡した値 | 保存された値 |
|---|---|
| `us-chicago-1` | `us-chicago-1` |
| `"us-chicago-1"` | `"us-chicago-1"`（引用符が残る） |
| `{"OCI_REGION":"us-chicago-1"}` | 同一 |

→ 設定は **`JETUSE_AGENT_CONFIG` という JSON オブジェクト1本**で渡し、コンテナ側
`packages/agent-containers/agent_env.py` が `os.environ` へ展開する。
副次的に「sensitive な map は `for_each` に使えず plan が停止する」制約も回避できる。

### 3. 画像タグを機能ごとに分けると壊れる

エージェント画像だけを差し替えて apply したところ、API コンテナは既存の `jetuse-api:latest` のままで
`/api/health` に `capabilities.agents` が出ず、アプリ側の新機能が丸ごと効いていなかった。
API とエージェントは invoke ステート（`project_ocid` 等）の契約を共有するため、
**API / Functions ルーター / 3SDK エージェントを単一の `image_tag` で束ねる**設計に変更した
（配布 zip 生成時に commit SHA へ固定）。

### 4. その他

- `oci raw-request` は `--query` / `--output table` を**無視する**。抽出は grep で行う。
- ACTIVE な Hosted Deployment は直接削除できず、Application 削除でカスケードされる。
- `inbound_auth_config` の domain URL が実在しないと Application は CREATING → **FAILED** になる。
- E2E ハーネス（`ops/e2e/`）の穴を2件修正した: ログイン後の固定8秒待ちでトークン交換前に API を
  叩いて 401 になる問題と、`tone.wav` が生成されず音声シナリオで異常終了する問題（README にも記載が
  無かった）。

## apply の経緯（注記）

検証中の apply は4回実行している。1〜2回目は上記 provider バグによる失敗で、
3回目（CLI 方式へ切り替え後）に成功、4回目は API 画像を本ブランチのビルドへ差し替えるための再 apply。
**方式変更後の構成では、スタック作成 → apply の1回で 3SDK が ACTIVE になる**ことを確認している。

## 残課題

- provider のバグは Oracle へ報告する（本レポートが再現手順を兼ねる）。修正版が出たら
  `terraform_data` + CLI から通常の resource へ戻せる。
- 旧 resource 型（`oci_generative_ai_hosted_application` / `_hosted_deployment`）を state に持つ
  スタックからの移行機構は入れていない。PORT-03 は未リリースで、その state を持つ利用者は
  存在しないため（本検証スタックのみ）。

## 再検証（2026-07-29 夕・レビュー指摘の修正後）

レビュー（review-8〜10）で作成・削除の判断ロジックを直したため、マージ前に**新しいスタックを
作り直して**もう一度回した（人間の指示による）。証跡は `runs/<run-id>/e2e/reverify/`。

| 項目 | 結果 |
|---|---|
| 配布 zip からのスタック作成 → apply | ✅ **一発で成功**（初回検証で必要だった再 apply が不要になった） |
| ホスト型アプリ3本 | ✅ ACTIVE。所有者タグ `jetuse-owner` と設定指紋 `jetuse-config` の付与を確認 |
| 既存機能の非回帰 | ✅ 39/39 PASS |
| 3SDK のエージェント実行 | ✅ 9/9 PASS |
| 無効化時のエージェント削除 | ✅ 残存 0 件（destroy 用スクリプト経路） |
| 無効化構成の縮退 | ✅ 8/8 PASS |
| destroy | ✅ 成功。Hosted 0件 / ADB TERMINATED / Container Instance DELETED |

### 再検証で見つかった不一致（1件）

1回目の縮退確認で `capabilities.agents` が `disabled` ではなく `unavailable` を返し、
全体の `ok` が false になった。**原因は API イメージが health 修正より前のビルドだったこと**で、
コードの誤りではない。API を作り直して再実行し 8/8 PASS。

これは「配布物のバージョンを揃えないと片方だけ古いまま動く」という本タスクで既に踏んだ問題の
再演でもある（だからこそ `image_tag` を統一した）。検証時も**アプリ側の変更を入れたら
イメージを作り直す**必要がある。

### 補足

- 検証では `image_tag` に検証用タグ（`port03-e2e2`）を指定した。既定の `latest` は `main` の
  ビルドを指すため、本ブランチのアプリ変更が入らない。マージ後は `release.yml` が
  同一 commit SHA で全画像を push するので、この指定は不要になる。

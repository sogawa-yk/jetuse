# Public版ワンクリックデプロイ 実機E2E再検証（2026-08-24）

`main` 先端（`e8e2cc9`）の公開 ZIP をクリーンなテナンシへ流し、ログイン〜チャット〜RAG まで通した記録。
前回（2026-07-28・`PUBLIC-DEPLOY-E2E.md`）の後、v0.2.0 をリリースした状態での再確認にあたる。

あわせて、このテナンシで発生していた **1日約¥125の継続課金の原因を特定**した。

## 検証環境

| 項目 | 値 |
|---|---|
| テナンシ | DEPLOYTEST（ホームリージョン `ca-toronto-1`／購読 `ap-osaka-1`・`us-chicago-1`・`ca-toronto-1`） |
| 実行者 | テナンシ管理者（`Administrators`） |
| デプロイリージョン | `us-chicago-1`（GenAI実証済リージョン） |
| コンパートメント | `jetuse-test` |
| デプロイ経路 | `orm-main` リリースの `jetuse-orm.zip`（=ボタンと同一物）を Resource Manager Stack として apply |
| 対象コード | `main` = `e8e2cc9`（ZIP の `image_tag=e8e2cc9b727ce0cf8979fece5eb3ceb8a73fddf7`） |
| スタック変数 | `prefix=jtv0824` / IAM・認証・Semantic Store・ホスト型エージェントすべて有効 / `hosted_agent_min_replica=0` / OpenSearch 無効 / ADB 2 ECPU |

## 結論

**公開スタックはそのまま動く。** plan 190リソース（0 change / 0 destroy）→ apply 成功（約17分）→
ログイン → チャット → RAG（出典付き）まで実機で通った。

v0.2.0 で入れた migration 不足検出（ER-0015）も期待どおり動作し、`schema: ok (applied 27 / expected 27)` を返した。
リリース時に「ORM 経路は `RUN_DB_BOOTSTRAP=true` で自動適用されるため正常な新規デプロイでは `behind` にならない」と
推論で書いた部分が、実機で裏づけられた。

## 段階ごとの結果

| 段階 | 結果 |
|---|---|
| plan | 成功（190 to add / 0 to change / 0 to destroy） |
| apply | SUCCEEDED（約17分。案内は10〜15分） |
| Autonomous Database | AVAILABLE（26ai / 2 ECPU） |
| Container Instance | ACTIVE（CI.Standard.E4.Flex） |
| Functions application | ACTIVE |
| SPA 配信 | HTTP 200 |
| 認証境界 | 未認証の `GET /api/health` は HTTP 401 |
| ログイン | 成功。**パスワード変更を要求されない**（`UserPasswordChanger` 経由の設定が効いている） |
| チャット | 成功（SSE。API GW → Container Instance → GenAI が疎通） |
| RAG | 成功（出典付きで正答） |
| destroy | SUCCEEDED（約7分） |

### `/api/health` の内容

```
ok: false
chat:    ok  — モデル11種すべて ok
rag:     ok
dbchat:  ok  — select_ai ok / sample_data ok / semantic_store は SEMSTORE_OCID 未設定
speech:  ok
ocr:     ok
tts:     unavailable — us-chicago-1 / us-phoenix-1 とも HTTP 404
agents:  ok  — openai_agents / langgraph / adk すべて ok
schema:  ok  — applied 27 / expected 27
```

総合 `ok: false` の原因は **TTS の1点のみ**。ホスト型エージェントは3SDKとも生きていた。

### RAG の実応答（証跡）

```
質問: アップロードした文書に書かれている検証IDを、そのまま1つだけ答えてください。
応答: 検証ID: **DEPLOYTEST-20260824**（出典: deploytest-20260824.txt）
```

## 継続課金の原因 — GenAI リソースは Terraform 管理外

このテナンシでは 8/10〜8/22 のあいだ **毎日ほぼ同額（¥124.99/日 ≒ 月¥3,750）** が発生していた。
usage-api で分解すると **全額が GenAI のストレージ課金**で、コンピュート・DB・ネットワークはゼロだった。

| コンパートメント | SKU | 課金量 | 金額/日 |
|---|---|---|---|
| `jetuse-test`（ACTIVE） | Vector Store Storage（B112577） | 120 GB-hr（＝5 GB 常時） | ¥78.12 |
| `jetuse-test` | File Store Storage（B112414） | 24 GB-hr（＝1 GB） | ¥15.62 |
| `jetuse-restricted`（**DELETED**） | Vector Store Storage | 24 GB-hr（＝1 GB） | ¥15.62 |
| 同上 | File Store Storage | 24 GB-hr（＝1 GB） | ¥15.62 |

実体は **Vector Store 6個**（すべて `jetuse-rag-demo`）で、実データは合計 **141KB** にすぎない。
単価は実測 **¥15.62/GB/日**、5ストアで ¥78.12 ちょうどになることから、
**ストアあたり 1 GB が最小課金単位**と見られる（実測からの推定。OCI の課金仕様そのものは未確認）。
**中身の大きさはほぼ関係なく、ストアが存在すること自体が課金される。**

### destroy 前後で数えた（推論ではない）

RAG を1回使うと、アプリが実行時に Vector Store（同じ `jetuse-rag-demo`）を作ることを確認した。
その状態で Destroy を実行し、前後で数えた。

| リソース | destroy 前 | destroy 後 | Terraform 管理 |
|---|---|---|---|
| Autonomous Database | 1 | 0 | あり |
| Container Instance | 1 | 0 | あり |
| Functions application | 1 | 0 | あり |
| **GenAI Vector Store** | 1 | **1（残存）** | **なし** |

Terraform 管理下はすべて消え、**Vector Store だけが残った**。
過去の検証を繰り返すたびに1個ずつ積み上がった結果が、今回見つかった5個である。

**これは公開版の利用者にも同じことが起きる**（スタックを消したのに GenAI の課金だけ残る）。

### 削除済みコンパートメントの中身は回収できない

`jetuse-restricted` は PUBLIC-IAM-02 の検証用に作られ、その後コンパートメントごと削除されている。
ところが中の Vector Store / File はいまも課金されており、**削除できない**。

```
GET    /vector_stores/vs_ord_zrl1…              → 200（一覧にも単体にも出る）
DELETE /vector_stores/vs_ord_zrl1…              → 404 NotAuthorizedOrNotFound
delete_generative_ai_project(<project OCID>)    → 404 NotAuthorizedOrNotFound
```

実行者は `Administrators` なので権限不足ではない。**コンパートメントが DELETED だと読めるが書けない**。
OpenAI 互換 API（データプレーン）とコントロールプレーンの両方で拒まれた。
残る ¥31.24/日 を止める手段は、現時点では **SR** しか見当がついていない（他の手段は未確認）。

**順序を間違えたことが原因**で、コンパートメントを消す前に中の GenAI リソースを消す必要があった。

## 見つかった問題

| # | 内容 | 影響 |
|---|---|---|
| F-1 | **TTS がこのテナンシで使えない**（us-chicago-1 / us-phoenix-1 とも HTTP 404）。`tips.md` の「2026-07-28 実測: us-chicago-1 可」と食い違うため、リージョンではなくテナンシ側の購読・提供状況の差と考えられる（未検証）。**新しい事象ではなく、同テナンシの PUBLIC-IAM-02（2026-07-30）でも 「TTS を除く」と記録されている**ため、約1か月再現し続けていることになる | `/api/health` の総合判定が `false` になる。TTS 不可を全体 NG として出してよいかは要検討 |
| F-2 | **アプリのフッタが `v0.1.0` のまま**。出所は `packages/web/package.json` の `version` で、v0.2.0 リリース時に更新されていない | 利用者の不具合報告で版がずれる |
| F-3 | **RAG のファイル状態が `processing` のまま返り続ける**。検索は成功しているのに `GET /api/rag/files` の `status` が35秒以上変わらない | UI で「処理中」が出続け、待たされていると誤解する |
| F-4 | **CLI でスタックを作ると `tenancy_ocid` / `region` が入らない**（plan が `No value for required variable` で失敗）。コンソールの Deploy ボタン経由では `schema.yaml` の hidden 変数として自動注入される | ボタン経路の欠陥ではないが、CLI で検証する手順には2変数の明示が要る |

## 後始末

- `jetuse-test`: Vector Store 6個（既存5＋検証で作られた1）と File 18件を削除。**現在 0件 / 0件**
- `jetuse-restricted`（DELETED）: Vector Store 1個・File 4件・GenerativeAiProject 2個が残存。**API から削除不能**
- 検証スタックは Destroy 済み

課金は **¥124.99/日 → ¥31.24/日**（残りは上記の回収不能分）。

# シナリオ3（① の限界測定）— 実施済み。想定と異なる結果

実環境: 実 OCI Vector Store（ap-osaka-1 / コンパートメント jetuse-dev）。
検証用に `jetuse-spike-m1-vs`（Vector Store）と `jetuse-spike-m1-project`（GenAI プロジェクト）を作成。
モックなし。

## 実行コマンド

```
PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_a_vector_store.py
PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_a_limits.py
```

生ログ全文: `method-a-vector-store.log` / `method-a-limits.log`

## 期待（タスク記載）

「実 OCI Vector Store に同じ架空チャンクを投入し、属性付与とフィルタ検索を試行。
できない場合はエラー内容そのものを証跡に残す」。

## 実結果 — **属性付与もフィルタ検索も「できた」**

タスクの前提（① では属性もフィルタも持てない）は実機で覆った。

- 属性付与: `vector_stores.files.create(..., attributes={file, version, sheet, cells, sha256,
  kind, current_version})` が受理され、`retrieve` でそのまま返る。
- フィルタ検索: `vector_stores.search(filters={"type":"eq","key":"current_version","value":"Y"})`
  および Responses の `tools[].filters` の双方で旧版が 0 件になる。
  フィルタ無しでは旧版 `c08`（score 0.8536）・`c09`（0.7261）が返る＝対照も成立。
- `file_search_call.results[]` は `attributes` / `text` / `score` / `file_id` / `chunk_id` を返す。

## できなかったこと（エラー本文をそのまま記録）

1. **`in` フィルタ**

```
Error code: 400 - {'error': {'code': 'invalid_value', 'message': 'Status Code from provider: 422,
Provider response: {"detail":[{"type":"missing","loc":["body","filters",
"function-after[validate_value_type(), ComparisonFilter]","value"],"msg":"Field required",
"input":{"type":"in","key":"version","values":["2.0"]}}, …
```

2. **属性のキー数・値長・型**

```
[キー 20 個]  Error code: 400 - Metadata must not contain more than 16 key-value pairs.
[値 600 文字] Error code: 400 - Metadata value for key 'long' exceeds max length of 512 characters.
[入れ子]      Error code: 400 - Metadata value for key 'nested' must be a string, number, or boolean.
```

3. **チャンク単位の属性は持てない**（これが ① の本質的な限界）

1 ファイル（4318 文字）を投入して 5 チャンクに割れた状態で検索すると、
5 チャンクすべてが同一の `cells=A1:Z999 sheet=全体` を返す:

```
このファイル由来のヒット（チャンク）数: 5
  chunk_id=3_263838af-… cells=A1:Z999 sheet=全体
  chunk_id=4_fd1096fa-… cells=A1:Z999 sheet=全体
  chunk_id=2_0ea41f1c-… cells=A1:Z999 sheet=全体
  chunk_id=0_48ed111a-… cells=A1:Z999 sheet=全体
  chunk_id=1_7bd23d88-… cells=A1:Z999 sheet=全体
=> 異なる attributes の種類: 1 （1 なら属性はファイル単位＝チャンクごとの cells は持てない）
```

4. **存在しないキーでのフィルタが静かに 0 件になる**（エラーにならない）

```
[存在しないキー] filters={"type": "eq", "key": "not_exists", "value": "x"}
  -> OK 0 件: []
```

5. **途中で踏んだ実機挙動**（属性の可否判定と混ざるので記録）

Vector Store は CP 作成完了後も DP へ伝播するまで files 系が 404 を返す:

```
Error code: 404 - {'code': '404', 'message': '{"error":{"message":
"VectorStore vs_kix_… with status Completed not found."…}}'}
```

初回の試行はこの 404 を「attributes 非対応」と誤判定していた。
DP 可視性を待つ処理を入れて再測し、上記の結論に修正した。

## この結果が判断に与える影響

「① では無理だから ③」という筋は成立しない。差は
**出典の粒度（ファイル単位 vs チャンク単位）と業務データとの結合可否**に移る。
ADR-0019（案）はこの実測に基づいて段階採用を提案している。

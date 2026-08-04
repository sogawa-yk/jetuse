# MEM-01 事前調査: Oracle が「Agent Memory」として提供しているものの実体

日付: 2026-07-31 / 実機: `jetuse-loop-adb`（ADB Serverless・ap-osaka-1・**Oracle AI Database 26ai
Enterprise Edition Release 23.26.3.1.0** / `compatible=23.5.0`）
検証スキーマ: `JETUSE_MEM01_EFC2EA`（この run 固有。共有 loop ADB をスキーマだけで隔離）
証跡: `runs/2026-07-31T0204_MEM-01/research/probe{1,3}-*.json` /
実行ログ `runs/2026-07-31T0204_MEM-01/e2e/research-run.log`
再現スクリプト: `spikes/mem01/probe_agent_memory.py`（辞書ビュー・読取専用。**必須の問い合わせが
失敗したら非ゼロ終了する** — 「問い合わせが通らなかった」を「機能が無い」と読み違えないため）/
`spikes/mem01/probe_sdk.py`（SDK の実動作。**期待値を assert し、外れたら非ゼロ終了する**）
認証モード: **`config_file` のみで実行**（`resource_principal` / `instance_principal` は未実行）

> この調査は「Oracle が Agent Memory を売っているから DB に機能があるはず」という前提を
> 実機で潰すために行った（SPIKE-10 の教訓）。**結論は当初の想定と違った。**

> **2026-08-01 追記**: この調査を受けた ADR-0022 は**保留**（実装しない）。
> 現行の ADR-0006 で要件は満たせており、ADB 側の明確な優位は「記憶を業務データと同じ場所で
> SQL から扱える」の 1 点だが、その要求が現時点で無いため。理由と再開条件は
> `docs/decisions/ADR-0022-agent-memory-backend.md` の「保留の判断」節。
> **検証用スキーマ `JETUSE_MEM01_EFC2EA` は削除済み**（証跡
> `runs/2026-07-31T0204_MEM-01/teardown/`）。再現するには `spikes/ragm02/setup_schema.py` を
> `SPIKE_SCHEMA_PREFIX=JETUSE_MEM01` で実行してスキーマを作り直すところから始める。

## 結論（3行）

1. **ADB 26ai の中に「Agent Memory」の PL/SQL API は無い。** 辞書ビューにも
   パッケージ仕様の本文にも該当物が 1 件も無い（下記①）。
2. Oracle が「Agent Memory」として実際に出しているのは **Python ライブラリ
   `oracleagentmemory`（26.6.0・作者 Oracle）** で、接続先スキーマに自前で表を作り、
   抽出・検索・削除まで担う（②）。DB のバージョン要件は 26ai 以上。
3. そのライブラリを **JetUse 既存の IAM 署名経路（OCI Generative AI）に載せて実機で動かした**。
   会話をまたいだ想起・subject 分離・スレッド削除での派生記憶の消滅が 3 つとも動いた（③）。

---

## ① ADB 26ai の中に Agent Memory の API は無い（実測）

`ADMIN` で辞書ビューを読み取り専用で走査した（`probe1-dictionary.json`）。

| 調べたもの | 結果 |
|---|---|
| `all_objects` で `%MEMOR%`（PACKAGE/PROCEDURE/FUNCTION/TYPE/VIEW/TABLE/SYNONYM） | 該当は **DB のメモリ管理**（`V$MEMORY_*` / `V$INMEMORY_*` / `V$VECTOR_MEMORY_POOL`）と **OCI SDK の型**（`DBMS_CLOUD_OCI_CORE_SHAPE_MEMORY_OPTIONS_T` 等）のみ。エージェント記憶に相当するものは **0 件** |
| `dictionary` で `%MEMOR%` / `%CONVERSATION%` / `%AI_AGENT%` | 記憶用のディクショナリ・ビューは **0 件** |
| `DBMS_CLOUD_AI_AGENT` のサブプログラム（47 件） | `CREATE_AGENT` / `CREATE_TASK` / `CREATE_TEAM` / `CREATE_TOOL` / `RUN_TEAM` / `SET_VARIABLE` / `GET_TEAM_STATE` 等。**記憶の API は無い** |
| `DBMS_CLOUD_AI` のサブプログラム | `CREATE_CONVERSATION` / `SET_CONVERSATION_ID` / `UPDATE_CONVERSATION` / `DROP_CONVERSATION` / `ADD_CONVERSATION_TAG` / `DELETE_CONVERSATION_PROMPT` はある（後述） |
| `all_source` に対する語句検索（`memor` / `remember` / `recall` / `long-term` / `short-term` / `persona`） | `DBMS_CLOUD_AI`（1,613 行）と `DBMS_CLOUD_AI_AGENT`（1,593 行）の**パッケージ仕様は非 wrap で全文読める**。上記語句の一致は **0 件**（`DBMS_VECTOR` 側の "vector index memory advisor" だけが引っかかる＝DB のメモリ管理） |

**`DBMS_CLOUD_AI` の conversation は「長期記憶」ではない。** 仕様本文（`create_conversation`）が
明記しているとおり、これは *SELECT AI* のセッションに紐づくプロンプト履歴で、属性は
`title` / `description` / `retention_days` / `tags` の 4 つ。`USER_CLOUD_AI_CONVERSATION_PROMPTS`
に prompt / prompt_response がそのまま入る。**要約も抽出も横断想起も無い**。
JetUse のチャット経路（Responses API）とも結び付いていない。

> 一次資料との関係: Oracle の Select AI Agent の資料は agent framework の説明として
> "memory" に言及する。しかし**この ADB（23.26.3.1.0）で API として露出しているものは無い**。
> 「機能の説明はあるが実体は確認できない」ので、**記載なし・実測のみ**として扱う。

## ② Oracle の「Agent Memory」は Python ライブラリだった

- 配布: `pip install "oracleagentmemory==26.6.0"`（PyPI・Author: Oracle・
  License: `Apache-2.0 OR UPL-1.0`）。前提は **Oracle AI Database 26ai 以上**、Python 3.10〜3.13。
- **中身は `.pyc` のみ**（`.py` ソースは同梱されない）。挙動の確認は実行と introspection でしかできない。
- 依存は `litellm<2,>=1.84.0` / `numpy` / `oracledb<5,>=3.4.2` / `pydantic` / `cryptography<50,>=46.0.7` /
  `aiohttp` / `anyio` / `urllib3`。
- 主な API: `OracleAgentMemory(connection=, embedder=, llm=, schema_policy=, memory_store_id=)` /
  `create_thread(user_id=, thread_id=)` / `thread.add_messages()` / `add_memory()` /
  `search(query, user_id=, agent_id=, thread_id=, exact_user_match=, metadata_filter=)` /
  `delete_thread()` / `delete_user(cascade=True)` / `delete_memory()` / `update_memory()` /
  `thread.get_context_card()` / `thread.get_summary()` / `wait_for_memory_extraction()`。

### DB に何を作るか（実測）

`schema_policy=CREATE_IF_NECESSARY` / `memory_store_id="MEM01"` で接続すると、**接続先スキーマに**
次を作った。

| 表 | 役割 |
|---|---|
| `MEM01_THREAD` | スレッド（＝会話）。`RECORD_ID` が主キー |
| `MEM01_MESSAGE` | 発話 |
| `MEM01_MEMORY` | 抽出された記憶。`MEMORY_TYPE`（`preference` / `fact` / `memory` …）・`THREAD_ID`・`USER_ID`・`METADATA`(JSON)・`EXPIRES_AT` を持つ |
| `MEM01_ACTOR_PROFILE` | user / agent のプロフィール（`add_user` / `add_agent`） |
| `MEM01_RECORD_CHUNKS` | 検索用のチャンクと `EMBEDDING`(VECTOR)。**HNSW ベクタ索引が自動で張られる** |
| `MEM01_ORACLEAGENTMEMORY_SCHEMA_META` | スキーマ版の管理 |

> **表名は引数によって変わる**。`memory_store_id="MEM01"` は `MEM01_THREAD`（区切りあり）、
> 廃止予定の `table_name_prefix="MEM01"` は `MEM01THREAD`（区切り無し）になる。
> 最初に後者で試して名前を取り違えたので、実測で確かめること。

**権限**: `CREATE SESSION` / `RESOURCE` / 表領域割当に加えて **`CREATE JOB` が要る**。
無いと期限切れレコードの掃除ジョブ作成が `ORA-27486` で落ちる。**落ち方は soft** で、
警告を出したまま初期化は成功し、**TTL の purge だけが動かない**（気づきにくい）。
`GRANT CREATE JOB` 後に再実行すると `MEM01_PURGE_EXPIRED_RECORDS_J`（`FREQ=DAILY;INTERVAL=1`・
`SCHEDULED`）が作られることを確認した。

### 記憶の抽出は誰がやるか（チケットの調査項目）

**ライブラリ側がやる。** `thread.add_messages()` の裏で LLM を呼んで記憶を抽出し、
`MEM01_MEMORY` へ入れる（非同期。`wait_for_memory_extraction(timeout=)` で待てる）。
`memory_extraction_config` / `memory_extraction_frequency` / `memory_extraction_window` /
`memory_extraction_custom_instructions` で挙動を変えられる。

- つまり **「自前バックエンドでは抽出を自分でやることになる」という前提は外れた**。
  自前で書くのは抽出プロンプトではなく、**LLM と埋め込みのアダプタ 2 つ（各 2 メソッド）**だけ。
- ただし**抽出の出力言語は安定しない**: 同じ日本語の発話から、
  `User prefers reports in bullet points`（英語）と `報告書は箇条書き`（日本語）の
  **両方が観測された**（前者は 4 発話、後者は 2 発話のスレッド）。記憶をユーザーに見せるなら
  `memory_extraction_custom_instructions` での指示が要る（未検証）。

### 認証: 既存の IAM 署名経路に載せられる（これが採否の分かれ目だった）

ライブラリ同梱の `Llm` / `Embedder` は **litellm** 経由で、OCI を使う場合は `oci/` プロバイダ＝
API キー（`OCI_USER` / `OCI_FINGERPRINT` / `OCI_TENANCY` / `OCI_KEY`）か `oci_signer` オブジェクトを要求する。
JetUse は API キーを持たない方針（ADR-0021）なので、そのままでは合わない。

`OracleAgentMemory` は `embedder` / `llm` に**インターフェース実装を直接渡せる**：

- `IEmbedder`: `embed(texts, is_query=) -> np.ndarray` / `embed_async` / `embedding_dimension` / `max_input_tokens`
- `ILlm`: `generate(prompt, response_json_schema=) -> LlmResponse(text)` / `generate_async`

この 2 つを **既存の `jetuse_core.embeddings`（`cohere.embed-multilingual-v3.0` / 1024 次元）と
`jetuse_core.genai`（OpenAI 互換 + IAM 署名）** で実装したところ、litellm を一度も import せずに
動いた（`litellm` は遅延 import なので、自前アダプタを使う限りロードされない）。
生成モデルは `meta.llama-3.3-70b-instruct`（`gpt-oss-120b` は Responses 専用で 404 になる）。

> **実行したのは `config_file` モードだけ**。アダプタは `jetuse_core.oci_auth` の
> 単一リゾルバを通るので `resource_principal` / `instance_principal` も同じ経路に載るはずだが、
> **この 2 つは未実行＝未検証**である。配備先で使う前に実機確認が要る。

## ③ 3 つの要求を実機で確認した

`spikes/mem01/probe_sdk.py`（証跡 `probe3-sdk-e2e.json` / ログ `e2e/research-run.log`）。
会話 A に日本語 4 発話、会話 B（同じ user・別スレッド）に別話題の 1 発話を入れて確認した。
**すべて assert してあり、外れればスクリプトが非ゼロ終了する。**

| 確認 | 結果 |
|---|---|
| 抽出 | 会話 A の 4 発話 → `MEM01_MEMORY` 3 行 / `MEM01_RECORD_CHUNKS` 7 行 |
| **会話をまたいだ想起** | 会話 B が存在する状態で `search(user_id="spike-user-a")` → `User prefers reports in bullet points` が返り、その **`thread_id` が会話 A** であることまで確認（出所つきで裏が取れている） |
| **subject 分離** | `search(user_id="spike-user-b", exact_user_match=True)` → **0 件** |
| **削除の伝播** | 両スレッド削除後: `MEM01_MEMORY` 4→**0** / `MEM01_MESSAGE` 5→**0** / `MEM01_RECORD_CHUNKS` 9→**0**。削除後の `search` も **0 件** |

ADR-0006 が OCI ネイティブ長期メモリで実機確認した「会話削除で派生記憶も消える」と
**同等の保証が、SQL で数えられる形で成立している**（削除は同じ DB の中なので行数で示せる）。

### 実装で踏みやすい 2 点（実測）

- **`search(record_types=...)` は記憶の種別で絞る。** 抽出された好みは `preference`、
  事実は `fact` になるので、`["memory"]` だけを指定すると **0 件**になる（実際に踏んだ）。
  記憶全体を対象にするなら `["memory", "preference", "fact", "guideline"]` を渡す。
- **`thread.get_context_card()` は既定で他スレッドの記憶を拾わなかった。**
  会話 B のカードには会話 B 由来の記憶しか入らず、`max_relevant_results=10` /
  `min_relevant_results_by_type` を広げても会話 A の好みは入らなかった。
  **会話横断の想起はストア側の `search(user_id=...)` を経路にすること。**

## 未検証・注意（顧客に言う前に確かめること）

- **`resource_principal` / `instance_principal` での動作は未実行**。配備先で使う前に要確認。
- **抽出の日本語化**（`memory_extraction_custom_instructions`）は未検証。
- ここで確かめたのは **SDK 直叩き**。JetUse の API を通した E2E は実装後（完了ゲート）に行う。
- ハイブリッド検索（ベクタ + テキスト）の有効化条件と、Oracle Text 索引に要る権限は未確認。
- `table_name_prefix` は 27.1 で廃止予定（`memory_store_id` を使うこと）という
  DeprecationWarning が出る。**API はまだ動く版が変わる**前提で扱う。
- ライブラリが `.pyc` のみで配布されるため、**中身の監査はできない**。
  作るオブジェクトと発行される DDL は実行して観測するしかない。

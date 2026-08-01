# ADR-0022: Agent Memory の ADB バックエンドは Oracle 公式 SDK（`oracleagentmemory`）に載せる

日付: 2026-07-31 / 状態: **Proposed（2026-08-01 の人間ゲートで「保留」。下記「保留の判断」）** /
関連: ADR-0020 §2（Accepted）, ADR-0006（**Accepted。棄却しない**）, ADR-0021（資格情報方針）,
検証: `docs/verification/MEM-01.md`

## 背景

ADR-0020 §2 で「Agent Memory のバックエンドとしても `adb` と同じ土台を使う」と決めた。
MEM-01 の起票時点の想定は **「Oracle 専用 API があるか、無ければ `VECTOR` 表 + 自前設計」**
の二択だった。着手前に ADB 26ai の実機で確かめたところ、**どちらでもなかった**。

- **ADB 26ai（23.26.3.1.0）の中に Agent Memory の PL/SQL API は無い。**
  辞書ビューにも、非 wrap で全文が読める `DBMS_CLOUD_AI` / `DBMS_CLOUD_AI_AGENT` の
  パッケージ仕様にも、記憶に相当するものが 1 件も無い。
  `DBMS_CLOUD_AI` の conversation は Select AI のプロンプト履歴で、抽出も横断想起も持たない。
- Oracle が「Agent Memory」として出しているのは **Python ライブラリ `oracleagentmemory`
  （26.6.0・Author: Oracle・Apache-2.0 OR UPL-1.0・要 ADB 26ai 以上）**で、
  接続先スキーマに自前で表と HNSW ベクタ索引を作り、LLM による抽出・検索・削除まで担う。

根拠と実行結果は `docs/verification/MEM-01.md`。

## 決定

**自前で記憶の表・抽出・想起を設計せず、Oracle 公式 SDK `oracleagentmemory` に載せる。**
JetUse 側が書くのは「JetUse の会話・ユーザーを SDK の thread・user へ写像する薄い層」と
「LLM / 埋め込みのアダプタ」だけにする。

### 1. LLM と埋め込みは既存の IAM 署名経路に載せる（API キーを持ち込まない）

SDK 同梱の `Llm` / `Embedder` は litellm 経由で、OCI では API キーか signer を要求する。
ADR-0021 で API キーを DB にも配備先にも置かない方針にしているので、これは使わない。
`OracleAgentMemory(embedder=, llm=)` は**インターフェース実装を直接受け取れる**ので、
`IEmbedder` / `ILlm`（各 2 メソッド）を既存の `jetuse_core.embeddings` /
`jetuse_core.genai` で実装する。**`config_file` モードで実機動作を確認済み**。
両モジュールは `jetuse_core.oci_auth` の単一リゾルバを通るので `resource_principal` /
`instance_principal` も同じ経路に載るが、**その 2 つは未実行＝未検証**（配備前に確認する）。

### 2. ADR-0006 の OCI ネイティブ長期メモリは既定のまま。選択式にする

記憶バックエンドを `oci_native`（既定・現行挙動）と `adb` から選べるようにする。
**既定では現行の挙動が一切変わらない**。ADR-0006 は棄却しない。

| | `oci_native`（ADR-0006・既定） | `adb`（本 ADR） |
|---|---|---|
| 実体 | プロジェクト LTM + 会話 metadata の `memory_subject_id` | ADB 内の表 + HNSW 索引（SDK 管理） |
| 抽出 | プラットフォーム側（設定のみ） | SDK が LLM で実行（モデル・指示を選べる） |
| 記憶の可視化 | API 無し（中身を出す口が無い） | **SQL で読める**（`MEMORY` 表を直接 SELECT できる） |
| 業務データとの結合 | 不可 | 同一 DB なので JOIN 可（未実証） |
| 削除 | 会話削除で派生記憶も消える（ADR-0006 実機確認） | `delete_thread` で messages / memories / chunks が 0 になる（実測） |
| 前提 | LTM 有効プロジェクト（**作成時のみ設定可**） | ADB 26ai 以上 + `CREATE JOB` + 依存追加 |

### 3. 依存の増加を受け入れる（ただし影響を明示する）

`oracleagentmemory` は `litellm<2,>=1.84.0` を**必須依存**に持ち、numpy / tokenizers /
huggingface-hub / tiktoken を連れてくる（インストール後で約 150MB）。自前アダプタを使う限り
litellm は遅延 import なので**実行時にはロードされない**が、**イメージには入る**。
既存の固定（`cryptography>=43` / `oracledb>=2.0`）との衝突は無い（実測: 49.0.0 / 4.0.2 で要件を満たす）。

### 4. `CREATE JOB` をスキーマ権限に足す

TTL の掃除ジョブ作成に要る。無いと `ORA-27486` で **警告のまま初期化は成功し、purge だけ動かない**
（気づけない失敗）。`ops/setup-dev-schema.py` と ORM の IAM/DDL に足す。

## 保留の判断（2026-08-01・人間ゲート）

**この ADR は保留とし、状態は `Proposed` のままにする。実装には進まない。**
上の「決定」節は**採用でも却下でもない**（提案としてそのまま残す。書き換えていない）。

### なぜ保留か

- **現行の ADR-0006（OCI ネイティブ長期メモリ）で要件は満たせている。**
  会話横断パーソナライズ・subject 分離・retention は既に動いている。
- **実測が示した ADB 側の 3 点（会話横断の想起・subject 分離・スレッド削除の伝播）は、
  同じ要件を満たしているだけで上回ってはいない。** 検証は成功したが、それは
  「置き換える理由」にはならない。
- **明確な優位は 1 点だけ**: 記憶を業務データと**同じ場所で SQL から扱える**
  （業務表との JOIN・行レベル制御・同一トランザクション）。
  **その要求は現時点で出ていない。**
- 対して代償ははっきりしている: 依存が約 150MB 増える（`litellm` が必須依存。
  自前アダプタを使えば実行時にはロードされないが、イメージには入る）/
  抽出の出力言語が安定しない（同じ日本語入力から英語と日本語の両方を観測）/
  ADB 26ai 以上が必須。

つまり **「いま入れる理由は無いが、調査は捨てるには惜しい」**。
調査結果（`docs/verification/MEM-01.md`）と再現スクリプト（`spikes/mem01/`）は残す。

### 再開する条件（どれか 1 つで再検討する）

1. **記憶を業務データと結合して見せる要求が出たとき。** 例:「この顧客の過去の会話から
   拾った好みを、契約表・在庫表と JOIN して提案に反映する」といったデモ・案件要件。
   これが本 ADR の唯一の明確な優位であり、現行方式では原理的に実現できない。
2. **記憶の中身を SQL で監査・開示する要求が出たとき。** ADR-0006 の残課題
   「抽出された記憶の内容をユーザーに開示する UI（記憶の透明性）」や、
   監査ログとして記憶を照会したい要件。現行方式には記憶を読み出す口が無い。
3. **記憶に行レベル制御（VPD / Data Redaction）や保持期間の細かい制御が要求されたとき。**
4. **ADR-0006 の前提が崩れたとき。** OCI ネイティブ長期メモリが使えない配備先
   （LTM 未設定のプロジェクトしか作れない・当該リージョンで提供されない等）に出す必要が生じた場合。

再開時に**再確認が要ること**（保留中に陳腐化しうる）: `oracleagentmemory` の版
（26.6.0 で検証。`table_name_prefix` は 27.1 で廃止予定）/ 依存サイズ /
`resource_principal` での動作（未検証）/ 抽出言語の制御可否。

## 却下した案とその理由

- **`VECTOR` 表 + 自前設計（起票時の想定）**: 却下。抽出・要約・チャンク化・ベクタ索引・
  TTL purge・カスケード削除を自前で持つことになり、公式実装と同じものを作り直す。
  「Oracle AI Database を Agent Memory の格納先として選べる」という要件に対して、
  **Oracle が出している実装を使わない理由が無い**。
- **`DBMS_CLOUD_AI` の conversation を長期記憶として使う**: 却下。抽出も横断想起も無く、
  Select AI のプロンプト履歴でしかない（実測）。JetUse のチャット経路とも結び付いていない。
- **SDK 同梱の litellm 経路（`oci/`）をそのまま使う**: 却下。API キーの持ち込みが要り、
  ADR-0021 が廃止した経路に戻る。
- **ADR-0006 を置き換える**: 却下（チケットの非ゴール）。既定は現行のまま。

## 影響

- `packages/api` に依存が 1 つ増える（+ 推移依存で約 150MB）。オプション extra にするかは実装時に判断。
- 新規モジュール `jetuse_core/memory_adb.py`（既存の長期メモリ経路には触らない）。
- 会話削除（`conversations.delete_conversation`）から SDK の `delete_thread` を呼ぶ経路が要る。
- `GET /api/capabilities` に記憶バックエンドの能力差を載せる（RAGM-03 と同じ枠組み）。
- ADB スキーマ権限に `CREATE JOB` を追加（`ops/setup-dev-schema.py` / ORM）。

## 未解決 / 後続で決めること

- **配備先の認証モード（`resource_principal` / `instance_principal`）での動作は未検証。**
- 抽出結果の**言語が安定しない**（同じ日本語入力から英語と日本語の両方を観測）。
  日本語で残すなら `memory_extraction_custom_instructions` の指示が要る（未検証）。
- **会話横断の想起はストア側の `search(user_id=...)` を経路にする。**
  `thread.get_context_card()` は既定でも設定を広げても他スレッドの記憶を拾わなかった（実測）。
- ハイブリッド検索（ベクタ + テキスト）の有効化条件と必要権限は未確認。
- SDK は `.pyc` のみで配布されるため**中身の監査ができない**。
  作る表・発行される DDL は実行して観測するしかない（本 ADR の根拠も実測）。
- `table_name_prefix` は 27.1 で廃止予定。SDK の版上げ追従の運用が要る。

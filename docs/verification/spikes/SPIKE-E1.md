# SPIKE-E1 (ENH-04): ADB 26ai の Select AI Agent フレームワーク実機調査

実施日: 2026-06-15 / 対象: ADB 26ai(jetuse-dev-adb) / 判定: **go(実機で作成〜実行を確認)**

## 結論

**Select AI Agent フレームワーク(`DBMS_CLOUD_AI_AGENT`)は ADB 26ai で利用可能**。
エージェント/ツール/タスク/チームの作成と、チーム実行(`RUN_TEAM`)まで実機で動作確認。
→ ADR-0009のエージェント抽象に「第4の実行種別=Select AI Agent(DBネイティブ)」を追加できる。

## 実機で確定したAPI(リバースエンジニアリング)

- パッケージ: `DBMS_CLOUD_AI_AGENT`(PUBLICシノニム。本体は C##CLOUD$SERVICE)。
  ディクショナリ: `USER_AI_AGENTS / USER_AI_AGENT_TOOLS / USER_AI_AGENT_TASKS /
  USER_AI_AGENT_TEAMS`(+ *_ATTRIBUTES / *_HISTORY)。
- **前提グラント(必須)**: `GRANT EXECUTE ON DBMS_CLOUD_AI_AGENT TO JETUSE_APP`。
  無いと `PLS-00201: identifier 'DBMS_CLOUD_AI_AGENT' must be declared`。
  → `ops/setup-select-ai.py` に追加済み(ADMINで実行)。JETUSE_APPは DBMS_CLOUD_AI のみ保有していた。
- 作成(attributesはJSON):
  - `CREATE_AGENT(agent_name, attributes=>'{"profile_name":"<Select AIプロファイル>","role":"..."}')`
  - `CREATE_TASK(task_name, attributes=>'{"instruction":"{query}"}')` ※`agent` 属性は不可(ORA-20051)
  - `CREATE_TEAM(team_name, attributes=>'{"agents":[{"name":"<agent>","task":"<task>"}],"process":"sequential"}')`
    ※process の有効値は `sequential`(`naive`/`hierarchical`は ORA-20053)
- 実行: **SQLコンテキストの関数呼び出し**
  `SELECT DBMS_CLOUD_AI_AGENT.RUN_TEAM(team_name=>'<team>', user_prompt=>:q) FROM dual`
  - **会話の事前設定が必須**: `id := DBMS_CLOUD_AI.CREATE_CONVERSATION();
    DBMS_CLOUD_AI.SET_CONVERSATION_ID(id);` を先に実行。無いと
    `ORA-01400: cannot insert NULL into ...CONVERSATION_PROMPT$.CONVERSATION_ID#`。
- 削除: `DROP_TEAM/DROP_TASK/DROP_AGENT(<name>)`。チーム作成は内部で `AGENT$<team>` という
  **Oracle-maintainedプロファイル**を作る(ユーザーからは DROP_PROFILE 不可)。同名で作り直すと
  `ORA-20046: Profile ... already exists` → チーム名は使い捨て/一意管理が無難。

## 未確定(実装時に詰める)

- **ツール(CREATE_TOOL)未設定だとエージェントは応答を反射するだけ**(実機: 上記の最小構成では
  プロンプトをそのまま返した)。実データ回答にはSQL/RAGツールの登録と agent/task への割当が必要。
  プリビルド/DBインスペクション系の有無もここで確認する。

## 実装方針(ENH-04・goの場合)

- エージェント作成画面の種別に「Select AI Agent」を追加(SDK選択=hostedコンテナ群と並列)。
  対象 Select AI プロファイル(SH or 本人データセット ENH-01)・ロール・ツールを設定。
- `jetuse_core/select_ai_agent.py`: agent/task/team の作成(冪等)＋会話設定＋`RUN_TEAM`実行のラッパ。
  main.py のエージェントrouting(framework分岐)に `select_ai` を追加。
- comparison: `docs/comparison/agent-runtimes.md`(hosted SDK 3種 vs Select AI Agent の使い分け)。

## 参照
ops/setup-select-ai.py(grant追加) / ADR-0009 / jetuse_core/nl2sql.py(Select AIプロファイル)

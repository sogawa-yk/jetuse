# ADR-0010: agent framework 値の正規化方針とレガシー値のDB移行

日付: 2026-06-18
状態: 承認（リファクタリング review-validation.md §1.5 / P0.6 の実施に伴い確定）

## 背景

`agents.framework` には歴史的経緯で複数世代の値が混在しうる:

- マイグレーション `010_agent_framework.sql` が `DEFAULT 'native'` で列を追加したため、既存行は `'native'` を持つ。
- ADR-0007/0008 期の in-process エンジン時代の値 `'agents_sdk'` も残存。
- ADR-0009（hosted ReAct 3SDK）移行後の現行 canonical 値は `Literal["openai_agents", "adk", "langgraph", "select_ai"]`（`service/main.py` の `AgentDefinition`、既定 `"openai_agents"`）。

入力(API)スキーマは現行 Literal のみ受理し、レガシー値は 422 で拒否する。一方で**読取時**には
`jetuse_core/hosted_agent.py` の後方互換マッピング `_LEGACY_SDK`/`normalize_sdk()` が
レガシー値を canonical へ読み替える（`service/main.py:488` で使用）。フロントにも同等の
読み替えが `pages/agentbuilder.tsx`・`pages/agents.tsx` に存在する。

実機 dev ADB（JETUSE_APP スキーマ・2026-06-18 時点）の分布を確認したところ、レガシー値が実在した:

| framework | 件数 | 区分 |
|---|---:|---|
| `agents_sdk` | 2 | レガシー → `openai_agents` |
| `native` | 2 | レガシー → `openai_agents` |
| `openai_agents` | 4 | canonical |
| `adk` | 1 | canonical |
| `select_ai` | 1 | canonical |

## 決定

1. **read-time `normalize_sdk()` / `_LEGACY_SDK` を正規化の唯一の正本とする**（API read 経路）。
   これは hosted ルーティングの現役コードであり**削除対象ではない**（review-validation.md §2-B 注記と整合）。
   マッピング: `agents_sdk` / `native` / `hosted` → `openai_agents`、`langgraph` / `adk` / `select_ai` / `openai_agents` はそのまま。

2. **DBレジ既存行のレガシー値はマイグレーションで一括 canonical 化する**。
   新規マイグレーション `012_normalize_framework.sql` を追加:
   ```sql
   UPDATE agents SET framework = 'openai_agents' WHERE framework IN ('native', 'agents_sdk', 'hosted')
   ```
   冪等（再実行で0行更新）。`langgraph`/`adk`/`select_ai` は対象外で温存。
   → dev ADB へ適用済み。適用後分布: `openai_agents`:8 / `adk`:1 / `select_ai`:1（レガシー値0）。

3. **入力(API)の Literal による拒否は現行のまま維持する**。レガシー値は API では受け付けない。
   万一 normalize 未適用の環境にレガシー行が残っても read-time `normalize_sdk()` が吸収する。

4. **フロントの読み替え（agentbuilder.tsx / agents.tsx）は当面残す**（多環境・防御的冗長化）。
   全環境で `012` 適用が確認できた段階で撤去可能。撤去時は本ADRを更新する。

## 影響・順序上の含意

- `normalize_sdk`/`_LEGACY_SDK` は P0.7（in-process エンジン `agents_sdk.py`/`langgraph_engine.py` 削除）の
  対象とは**別物**。P0.7 で消えるのはインプロセス実装であって normalize でも `langgraph` framework 値でもない。
- 一元化箇所: API 側は `jetuse_core/hosted_agent.normalize_sdk`。これが正本。フロントの読み替えはミラー。

## 参照
ADR-0009（hosted ReAct 3SDK）/ review-validation.md §1.5・§2-B / migrations `010`,`012` /
`jetuse_core/hosted_agent.py`（`_LEGACY_SDK`/`normalize_sdk`）

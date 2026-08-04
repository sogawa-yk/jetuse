# AGT-03 検証レポート: Agent Builder

日付: 2026-06-11
仕様: specs/11-agents.md [AGT-03]
状態: **実機E2E完了**（イメージ0.16.0、migration 007、SPA同時デプロイ）。ビルダーUI操作感はユーザーレビュー待ち

## 実装

- `AGENTS`テーブル（owner分離+public共有、migration 007）+ CRUD API（モデル/ツール/MCP所有チェックの検証付き）
- **Project割当による記憶分離**: エージェントに `project_ocid` を割当てると、そのエージェントの会話・記憶は指定Projectに分離（`make_inference_client(project_ocid=...)` を会話作成/responsesへ伝搬）。選択肢は `GET /api/agents/projects`（ACTIVE一覧）。プロジェクト新規作成はUI対象外（LTM等は作成時のみ — ADR-0006教訓）
- チャット統合: `ChatRequest.agent_id` でサーバー側解決 → instructions（system先頭付与）・モデル上書き・ツール/MCP・Projectを適用。共有エージェントのMCP（所有者の私有資源）は非所有者には適用しない
- UI: ホームにエージェントカード（公開共有含む）+「エージェントを作る」→ `/agents/new|/{id}` ビルダー。`/chat?agent={id}` で起動（バッジ表示、モデル/⚙/🛠はエージェント定義で固定）

## 実機E2E（API GW経由、イメージ0.16.0）

| ケース | 結果 |
|---|---|
| CRUD+検証（未知ツール422 / chat系モデル+ツール422 / 他人private 404） | pytest 62件パス |
| instructions反映（執事エージェント） | 「絶好調**でございます**。」— 人格が反映 |
| モデル上書き | リクエストllama指定でもエージェント定義のgpt-ossが使用される |
| **Project分離** | 分離Project割当エージェントに既定領域の長期記憶が漏れない（分離Projectが未ACTIVE時の「Invalid OpenAI project」エラーもヘッダ切替の証左） |
| プロジェクト一覧API | jetuse-dev-project / jetuse-dev-project-iso 返却 |

## 備考

- 分離用サンプルとして `jetuse-dev-project-iso`（LTMなし）を常設。エージェントの会話はOCI側ステートレス（短期メモリのConversationは既定チャットのみ。エージェント会話への適用は将来課題）

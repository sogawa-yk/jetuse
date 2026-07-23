# ENH-04: Select AI Agent をエージェント種別に統合

実施日: 2026-06-15 / 前提: SPIKE-E1(go)

## 実装
- `jetuse_core/select_ai_agent.py`: 一意名(owner+agent_id ハッシュ)で SQLツール/エージェント/タスク/
  チームを冪等構築(`_team_exists`=AGENT_TEAM_NAMEで判定)→会話設定→`RUN_TEAM`実行→会話破棄。
  ツール: `{"tool_type":"SQL","tool_params":{"profile_name":"JETUSE_SQL_AI"}}`。RUN_TEAMはSQL関数で
  team名はリテラル(named引数バインドはORA-00904)。
- main.py: `framework=select_ai` ルーティング(keepalive付きで RUN_TEAM 待機→1 deltaで返す)。
  AgentDefinition.framework に select_ai 追加(コンテナツール制約は非適用)。エージェント削除時に drop。
- UI: agentbuilder の種別に「Select AI Agent（DBネイティブ）」追加。agents一覧/ラベルも対応。

## 検証(ローカル実機)
- `select_ai_agent.run` で「販売チャネル別売上トップ3」→ Direct Sales 57,875,260.6 / Partners
  26,346,342.32 / Internet 13,706,802.03 を回答。冪等再実行(別質問=商品カテゴリ別)も既存チーム再利用で成功。`drop`でクリーンアップ。
- build/lint/ruff グリーン。

## 残/留意
- v1のプロファイルは JETUSE_SQL_AI(SHサンプル)固定。本人CSVデータセット(ENH-01)プロファイルや
  プリビルド/DBインスペクション系の選択はフォローアップ。
- チーム作成は内部で Oracle-maintained プロファイル AGENT$<team> を作る(同名再作成不可 → 名前は一意)。

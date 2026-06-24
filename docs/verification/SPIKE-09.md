# SPIKE-09: Responses APIツール機構検証（AGT-01前提）

実施日: 2026-06-11 / 実行: `spikes/spike09_agent_tools.py` / モデル: openai.gpt-oss-120b

## 結果サマリ

| 機構 | 結果 |
|---|---|
| カスタムfunctionツール | ✅ 完全動作（定義受理→function_call→output提出→最終回答） |
| ストリーミングイベント | ✅ `response.function_call_arguments.delta/done` + `response.output_item.added/done`（function_callはitem.doneで完成形が取れる） |
| code_interpreter built-in | ✅ **動作**（`{"type":"code_interpreter","container":{"type":"auto"}}`。Pythonで素数25個を正しく計算、output内に `code_interpreter_call` アイテム） |
| web_search built-in | ❌ **不可**: `Tool(s) [web_search] are only supported for OpenAI provider models` → Web検索はカスタムfunctionツールで自前実装する |

## 実機確定事項（実装に直結）

1. function_callアイテム: `{type, name, arguments(JSON文字列), call_id}`。ツール結果は次のリクエストのinputに **`{type:"function_call_output", call_id, output}`** を（元のfunction_callアイテムと共に）含めて提出する
2. ストリーミングでは `response.output_item.done` でfunction_callの完成形（name+arguments全体）が取得できる — デルタ結合不要
3. reasoningアイテムがfunction_call前後に挟まる（gpt-oss）。UIでは無視してよい
4. code_interpreterはサーバーサイド実行（OCI側サンドボックス）。ローカル実行リスクなし

## AGT-01設計への反映

specs/11-agents.md 参照（承認フロー2モード: 都度承認=ストリーム分割 / 自動実行=サーバー側マルチホップ）

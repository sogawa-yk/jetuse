# fn/ — OCI Functionsハンドラ置き場（ADR-0005）

非ストリーミングAPI（会話履歴CRUD・ファイル前処理・STT/TTS起動など）はここにFunctionとして実装する。
共通ロジックは `jetuse_core` を使うこと（二重実装の禁止）。

- 個々のfunctionはPhase 2のタスク（CHAT-02等）でデプロイ定義（func.yaml + fdk）ごと追加する
- 応答6MB上限のため、ファイルダウンロード系はObject StorageのPARを返す設計にする
- デプロイ先ApplicationはINFRA-01の `{prefix}-fnapp`

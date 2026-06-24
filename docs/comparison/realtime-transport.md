# 比較: リアルタイム文字起こしのクライアント⇔API転送方式（VOICE-02）

OCIリアルタイムSTT（WebSocket、IAM署名必須）をブラウザへ届ける経路の選択。
前提: SPA/APIはAPI Gateway配下（ADR-0004/0005）、**API GatewayはWebSocket非対応**
（[公式 Overview](https://docs.oracle.com/en-us/iaas/Content/APIGateway/Concepts/apigatewayoverview.htm) — HTTP/Sのみ。デプロイメントのルート定義にWSタイプ自体が無い）。

| 方式 | 経路 | 評価 |
|---|---|---|
| ① ブラウザ→OCIリアルタイムへ直接WS | SPA→`wss://realtime.aiservice...` | ✗ IAM署名（リクエスト署名 or セッショントークン）をブラウザに置けない。CORS/資格情報配布の問題で不採用 |
| ② API GW経由WS中継 | SPA→GW→CI(WS)→OCI | ✗ **GWがWSを通さない**（プロトコル非対応）。GWを外してCIを公開する案はTLS/認証/経路管理を自前化することになり本プロトタイプの構成から逸脱 |
| ③ チャンクPOST + SSE中継（**採用**） | SPA→GW→CI: 音声=POST(250ms粒度) / 結果=SSE。CI→OCI: `oci-ai-speech-realtime`(WS) | ○ GWの制約内で完結。SSEはSPIKE-02以降この経路で実証済み。**WhisperリアルタイムはpartialなしでWS双方向の利点が薄く**、結果遅延は元々秒単位（SPIKE-06）のためPOST粒度250msの追加遅延は無視できる |

## 採用方式の含意・制約（定量含む）

- 追加レイテンシ: 音声チャンクのHTTP往復（同リージョン、実測数十ms）+ バッファ250ms。
  Whisperリアルタイムのfinal確定自体が発話区切り後1〜3秒（SPIKE-06実測）のため支配項にならない
- セッション状態（OCI側WS接続）はAPIプロセス内保持 → **Container Instance 1台構成が前提**。
  水平スケール時はセッションアフィニティかRedis等の外部化が必要（プロトタイプ範囲外、backlog）
- GW経由SSEの間欠切断（既知、backlog #12）はクライアント再接続で吸収
- 帯域: 16kHz/16bit mono = 32KB/s。POSTオーバーヘッド込みでも実用上問題なし

## 顧客提案時の使い分け

- リアルタイム性が最重要（コールセンターのエージェントアシスト等）でpartialが必須の場合は、
  Whisper以外のORACLEモデル（partial対応）+ WS非経由の専用経路（プライベートLB直結等）を検討
- 議事録用途（確定テキストが取れればよい）は本方式で十分。バッチSTT（VOICE-01）との
  使い分けは「その場で見たい」か「後でまとめて」か

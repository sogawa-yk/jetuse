# SPIKE-02: ストリーミング経路検証（API Gateway越しSSE）

実施日: 2026-06-10 / リージョン: ap-osaka-1 / 実行: `spikes/spike02_measure_sse.py`, `spikes/sse_app/`

## 目的

OCI API Gateway経由のSSEについて ①バッファリングの有無 ②タイムアウト上限 ③切断時の挙動 を実測し、SSE経路（API GW vs LB直結）を決定する。

## 構成（すべて jetuse-spike- プレフィックス）

```
このインスタンス --HTTPS--> API Gateway (public subnet, readTimeout=300)
                              --HTTP--> Container Instances 10.0.1.129:8000 (private subnet)
                                          FastAPI: /drip(1秒間隔SSE) /burst(0.2秒間隔SSE)
```

- イメージ: `kix.ocir.io/<ns>/jetuse-spike-sse:v1`（podman build → OCIR push）
- CI: CI.Standard.E4.Flex 1ocpu/4GB
- ネットワーク: 既存セキュリティリストは変更せず、`jetuse-spike-nsg`（8000/tcp from 10.0.0.0/16、443/tcp from 0.0.0.0/0）をCI VNICとAPI GWに付与
- 計測方法: SSEイベントにサーバー送信時刻を埋め込み、クライアント到着時刻との相対差（初回イベント基準）でバッファリング遅延を分離計測

## 結果

| テスト | 結果 | 相対遅延 max |
|---|---|---|
| 直結 burst（0.2秒間隔×20） | 20件全受信 | 0.000s |
| **API GW経由 burst** | 20件全受信 | **0.000s（バッファリングなし）** |
| API GW経由 60秒連続（1秒間隔） | 60件全受信、60.12sで完走 | 0.000s |
| **API GW経由 330秒連続（readTimeout=300超）** | **330件全受信、330.56sで完走** | 0.013s |
| クライアント強制切断（5秒で切断） | GW/バックエンドともエラーなし。サーバー側は応答継続後に正常終了 | - |

### 確定事項

1. **API GatewayはSSE/chunked応答をバッファリングしない**。チャンクは即時フラッシュされ、LLMストリーミングのUXは直結と同等。
2. **`readTimeoutInSeconds` は「読み取り間隔」のタイムアウト**であり総時間の上限ではない。1秒間隔でイベントが流れ続ける限り、300秒を超えるストリーミングも切断されない（330秒で実証）。LLM応答は常にトークンが流れるため実用上問題なし。**注意: モデルの思考時間等で無通信が readTimeout を超えると切断される**ため、本実装ではコメントイベント（`: keepalive`）を15〜30秒間隔で挿入する。
3. デフォルトの readTimeout は10秒のため、**デプロイ仕様で明示的に300へ引き上げることが必須**（今回のスペックJSONを `infra/` に流用可能）。
4. ConnectTimeout初期トラブルはAPI GWへの443がセキュリティルールで閉じていたことが原因（NSGで解決）。Terraformモジュール設計時に「API GW用NSG: 443 ingress」を標準部品化する。

## 判定（ADR-0003）

**SSE経路はAPI Gateway経由を採用**。LB直結の代替検証は不要。API GWを採用することで認証（JWT検証ポリシー）、レート制限、CORS、IP制限（Phase 8）を同一レイヤで実装できる。

## 残課題

- 同時多重ストリーミング（数十接続）の挙動はPhase 2の負荷確認で実施
- API GWのJWT認証ポリシー併用時のSSE挙動（INFRA-02で確認）
- WebSocket（リアルタイムSTT中継）はAPI GW非対応のため、音声機能はLB直結 or クライアント直接続を別途設計（VOICE-02）

## 残置リソース

`jetuse-spike-apigw` / `jetuse-spike-sse-dep` / `jetuse-spike-ci` / `jetuse-spike-nsg` / OCIRリポジトリ `jetuse-spike-sse`
エンドポイント実値は `.env` の `APIGW_ENDPOINT` 参照（レポートには記載しない）。

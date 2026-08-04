# VOICE-02 検証レポート: リアルタイム文字起こし（チャンクPOST+SSE中継）

- 日付: 2026-06-12 / ブランチ: `task/voice-02` / 仕様: `specs/12-voice.md`
- 方式決定: **API GatewayはWebSocket非対応**（公式）→ クライアント⇔APIは「音声=チャンクPOST / 結果=SSE」の中継。
  比較ドキュメント: `docs/comparison/realtime-transport.md`

## 実装

| 層 | 内容 |
|---|---|
| コア | `jetuse_core/stt_realtime.py`: プロセス内セッション管理（1ユーザー1本・全体上限4・無操作120秒で自動close）。OCIへは `oci-ai-speech-realtime`（WS、RP/ユーザー署名両対応）。WHISPER制約（SPIKE-06: model_type=WHISPER、partial系パラメータ送信不可）を遵守。**分かち書きはVOICE-01と同じ結合処理をlistenerで適用** |
| API | `POST /api/stt/sessions` / `POST .../audio`（生PCM、64KB上限）/ `GET .../events`（SSE）/ `DELETE` |
| Web | `/realtime` ページ: getUserMedia→AudioContext(16kHz)→AudioWorklet(float32→int16)→250msごとに直列POST。SSEでfinal行をタイムスタンプ付き追記、全文コピー。「partialなし・数秒遅れ」の注記。ナビ追加 |
| 依存 | `oci-ai-speech-realtime>=2.0` を pyproject に追加（イメージ0.20.0） |

## E2E結果

1. **ローカル**（ユーザー認証）: 16kHz WAV（SPIKE-06のmeeting1_16k.wav）を実時間ペースで250msチャンクPOST →
   SSEで3つのfinalを受信。結合処理後のテキストは「本日の定例会議を始めます」等のクリーンな日本語
2. **実環境**（GW経由・M2Mトークン・Container Instance=リソースプリンシパル）: **一発成功**。
   同一WAVで final 3件 / 全体21.4秒（音声約12秒+確定待ち8秒。中継起因の遅延は実測上無視できる）
   - リアルタイムセッションは既存の `manage ai-service-speech-family` ポリシーでカバーされる
     （バッチで必要だった bucket/tag-namespaces はリアルタイムでは関与しない — ストレージを使わないため）
3. ユニットテスト4件（作成/送信/イベント中継/owner分離、同一ownerの置き換え、idle掃除、上限）→ API計73件pass
4. UI描画はPlaywrightで確認（`img/VOICE-02-realtime-ui.png`）。**マイク実機はヘッドレスでは検証不可** —
   ブラウザでの最終確認は人間確認項目（GW経由のSSE/POST経路自体は上記2で実証済み）

## 制約・既知事項

- セッション状態はAPIプロセス内 → **CI 1台構成が前提**（水平スケール時は外部化が必要。comparison参照）
- Whisperリアルタイムにpartialは無い（SPIKE-06）— UIに注記済み
- GW経由SSEの間欠切断（backlog #12）はこの画面でも起こりうる。発生時はセッション張り直し（開始ボタン）

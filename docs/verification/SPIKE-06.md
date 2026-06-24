# SPIKE-06: OCI Speech 検証

実施日: 2026-06-10 / リージョン: ap-osaka-1（TTSのみus-phoenix-1） / 実行: `spikes/spike06_realtime_stt.py` ほかOCI CLI

## 目的

①バッチ文字起こし（日本語・話者分離）②リアルタイムSTT WebSocket ③TTS日本語品質を実機確認し、音声チャット半二重パイプライン（録音→STT→LLM→TTS）の成立性とUX制約を判定する。

テスト音声: gTTSで合成した日本語会議風音声2本（mp3、計約25秒）。`jetuse-spike-speech` バケット使用。

## 結果1: バッチ文字起こし（大阪・成功）

- `oci speech transcription-job create` + `modelType: WHISPER_MEDIUM, languageCode: ja, diarization有効` → **SUCCEEDED**（2ファイル、数十秒で完了）
- 出力JSON: `transcriptions[].transcription`（全文）+ `tokens[]`（token/startTime/endTime/confidence/**speakerIndex**）+ `speakerCount`。話者分離フィールドが日本語Whisperでも出力されることを確認
- 精度: 「デザインレビュー→デザインデビュー」の1箇所誤り以外は正確（合成音声品質を考えれば良好）
- **注意: 出力は分かち書きトークン**（`本 日 の 定 例 会 議...`）。表示用には `tokens` を結合して空白除去する後処理が必要（日本語はWORD単位スペース挿入される）

## 結果2: リアルタイムSTT（大阪・成功）

`wss://realtime.aiservice.ap-osaka-1.oci.oraclecloud.com/ws/transcribe/stream` に `oci-ai-speech-realtime` ライブラリ（IAM署名認証）で接続し、16kHz mono PCMを実時間ペースで送信:

```
[connect_message] {'event': 'CONNECT', 'sessionId': '...'}
[result] final=True text=本 日 の 定 例 会 議 を 始 め ます
[result] final=True text=まず 先 週 の 進 捗 です が
[result] final=True text=バ ック エ ンド の AP I 実 装 は 予 定 通 り 完了 しました
```

実機で確定したWHISPERモードのクエリパラメータ制約（公式未文書、400エラーの実測）:

| パラメータ | 値 |
|---|---|
| `modelType` | `WHISPER`（`WHISPER_MEDIUM` は無効値） |
| `shouldIgnoreInvalidCustomizations` | WHISPERでは送信不可（400） |
| `finalSilenceThresholdInMs` | WHISPERでは送信不可（400） |
| partial results | **来ない**（finalのみ、ドキュメントどおり）。発話区切りごとに数秒遅れでfinalが届く |

## 結果3: TTS

- **大阪: 提供なし**（voice list / synthesize とも404）→ ドキュメントどおりPhoenix限定
- **Phoenix: 日本語ボイス5種を確認**（Aiko / Hana / Sakura / Yuki / Satoshi、いずれもTTS_2_NATURAL・24kHz）。計画書の「日本語TTS無し」想定は誤りで、**クロスリージョン呼び出しで日本語音声チャットが成立する**
- 実測: 40文字の日本語文を `voiceId: Yuki, languageCode: ja-JP` で合成 → **1.3秒**でMP3取得（このインスタンス→Phoenix往復込み）
- ハマり点: `modelDetails` に `languageCode: "ja-JP"` を含めないと「Yuki is not a valid voice id」エラー（英語ボイスのみのallowlistと比較される）

## 音声チャットv1のUX制約リスト（設計判断材料）

1. **リアルタイムSTTにpartialが無い** → 「話しながら文字が出る」UXは不可。発話終了→数秒後にfinal表示。半二重（押して話す）UIが前提
2. **TTSはPhoenix往復**（+1.3s/文）→ LLM応答をセンテンス単位で分割しTTSをパイプライン化すれば体感を改善可能
3. バッチSTTは高速・話者分離つき → **議事録機能を先行させる判断は妥当**。ただし音声チャットも技術的には成立する（録音→STT(大阪)→LLM(大阪)→TTS(Phoenix)）
4. 入力音声はブラウザでPCM 16kHz monoに変換して送る（MediaRecorder→AudioWorklet）

## 設計への影響

- VOICE-01（議事録）: バッチSTT + speakerIndex で実装可能と確認。Whisperの分かち書き後処理ユーティリティが必要
- VOICE-03（音声チャットv1）: 成立可能。TTSクロスリージョン依存を `.env` でリージョン設定可能にする
- リアルタイムSTTのWHISPERパラメータ制約をサービス層でバリデーションする

## 残置リソース

- バケット `jetuse-spike-speech`（入力2 mp3 + 出力JSON）
- 検証音声・TTSサンプル: `spikes/data/*.mp3`（リポジトリにはコミットしない: .gitignoreにdata音声追加）

# VOICE-03 検証レポート: 音声チャットv1（半二重: 話す→STT→LLM→TTS）

- 日付: 2026-06-12 / ブランチ: `task/voice-03` / 仕様: `specs/12-voice.md`

## 実装

| 層 | 内容 |
|---|---|
| TTS API | `POST /api/tts` `{text(≤500字), voice}` → mp3。`jetuse_core/tts.py` = Phoenixクロスリージョン（設定 `TTS_REGION` 既定us-phoenix-1）、RP/ユーザー署名両対応、ボイスはSPIKE-06確認済み日本語5種のallowlist |
| STT | VOICE-02のセッション基盤を再利用（ページ側で作成・録音停止時に無音1秒を送ってfinal確定を促進、最大5秒待ち） |
| LLM | `/api/chat/stream` をステートレスに利用。systemプロンプトで「読み上げ前提・話し言葉・記号/箇条書き回避・2〜3文」を指示 |
| パイプライン | LLMのdeltaを `。．！？!?\n` でセンテンス分割→確定文から順にTTSをfetch先行発行、再生はHTMLAudioElementで順序保証。停止ボタンで世代カウンタによりキュー破棄 |
| UI | `/voicechat` ページ（ナビ「音声チャット」追加）: 話す→停止して送信のトグル、状態バッジ（録音中/認識中/応答中/再生中）、会話バブル、読み上げON/OFF、ボイス選択 |

## E2E結果

1. **TTSローカル**（ユーザー認証）: 1.1秒で合成。**既定出力はWAV（RIFF 24kHz PCM、1文272KB）だった**
   → `speech_settings.output_format="MP3"` 明示で23KB（約1/12）。SPIKE-06の「MP3取得」はCLI側の指定によるもので、SDK既定はWAV
2. **TTS実環境**（GW経由・Container Instance=RP、**大阪→Phoenixクロスリージョン**）: **一発成功**。
   1.08秒・17KB mp3。リソースプリンシパルのクロスリージョン呼び出しは追加IAMなしで動作
   （ポリシーはリージョンスコープを持たないため `manage ai-service-speech-family` でカバー）
3. 不正ボイスは422。ユニット/既存テスト 73件pass、web build/lint成功
4. UI描画はPlaywright確認（`img/VOICE-03-voicechat-ui.png`）。
   **マイク→応答→読み上げの全ループはブラウザ実機の人間確認項目**（STT経路はVOICE-02で、TTS/LLMは本レポートで実証済み）

## 体感性能の見積り（実測ベース）

発話停止→再生開始 = STT final確定（1〜3秒）+ LLM TTFT（gpt-oss 約1.3秒）+ 1文目のセンテンス確定+TTS（約1.1秒）
≒ **4〜6秒**。センテンス単位のパイプライン化により2文目以降は途切れず再生される設計（SPIKE-06制約2への対策）。

## 制約・既知事項

- v1は日本語のみ（TTSボイスが日本語5種。英語ボイスの実機確認は未実施）
- 半二重: 再生中はマイクを開かない。会話履歴はページ内のみ（DB永続化なし）
- GW経由SSEの間欠切断（backlog #12）はLLM応答で起こりうる（クライアントは停止扱い）

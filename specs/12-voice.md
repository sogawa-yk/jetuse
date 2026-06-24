# specs/12: Phase 8 音声（VOICE-01〜03）

SPIKE-06（docs/verification/SPIKE-06.md）の実機確定事項を前提とする:

- バッチSTT: `WHISPER_MEDIUM` + `languageCode` + 話者分離（`tokens[].speakerIndex`）が大阪で動作。**出力は分かち書きトークン**（結合・空白除去の後処理必須）
- リアルタイムSTT: `modelType=WHISPER`（`WHISPER_MEDIUM`不可）、partialなし（finalのみ）、`finalSilenceThresholdInMs`等は送信不可
- TTS: **Phoenix限定**（日本語ボイス: Aiko/Hana/Sakura/Yuki/Satoshi、約1.3s/文）

## [VOICE-01] 議事録生成

### 目的
音声ファイルからバッチ文字起こし（話者分離つき）を行い、LLMで議事録/FAQ/記事に整形する。

### 前提（人間の事前作業）
- **IAM追加が必要**: Container Instance（動的グループ `jetuse-dg`）にSpeech権限がない。
  `allow dynamic-group jetuse-dg to manage ai-service-speech-family in compartment jetuse-proto`
  （docs/setup/iam.md「VOICE-01」節。追加されるまで実環境ではジョブ作成が401/404相当で失敗 → 503で案内）
- ローカル開発（`~/.oci` ユーザー認証）はSPIKE-06実証済みのため検証可能

### API
| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/minutes` | 自分のジョブ一覧（id/title/status/created_at） |
| POST | `/api/minutes` | multipart音声アップロード → バケット保存 → transcriptionジョブ作成。`{id, status}` を返す |
| GET | `/api/minutes/{id}` | 状態取得。OCIジョブをポーリング同期し、SUCCEEDED時は整形済みトランスクリプトを返す |
| DELETE | `/api/minutes/{id}` | レコード削除（バケットの音声/結果も削除、ベストエフォート） |
| POST | `/api/minutes/{id}/generate` | `{template: minutes\|faq\|article, model?}` → SSEで整形文書をストリーミング |

- 受理形式: mp3 / wav / m4a / ogg / webm、最大100MB（Speech APIの対応形式に準拠）
- バケット: Terraform既存の `jetuse-dev-speech`（設定 `SPEECH_BUCKET`、空なら機能無効=503）
- 入力: `minutes/{owner}/{id}/audio.{ext}`、出力prefix: `minutes/{owner}/{id}/out/`
- ジョブ: `WHISPER_MEDIUM` / `languageCode`（既定 `ja`、`en`等を選択可） / diarization有効（話者数自動）
- 後処理（SPIKE-06の分かち書き対策）: `tokens[]` を `speakerIndex` の連続区間でutteranceにまとめ、
  日本語はトークン結合後に空白除去、英語等はスペース結合。`{speaker, start, end, text}` の配列としてCLOB保存
- 状態遷移: `uploading→processing→completed|failed`（OCI側 ACCEPTED/IN_PROGRESS→processing）

### DB（migration 009）
```sql
CREATE TABLE minutes_jobs (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  title VARCHAR2(400) NOT NULL,          -- 元ファイル名
  status VARCHAR2(20) DEFAULT 'processing' NOT NULL,
  language VARCHAR2(10) DEFAULT 'ja' NOT NULL,
  audio_object VARCHAR2(700) NOT NULL,   -- バケット内オブジェクト名
  oci_job_id VARCHAR2(128),
  duration_sec NUMBER,
  speaker_count NUMBER,
  transcript CLOB,                       -- utterance配列のJSON
  error VARCHAR2(1000),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);
CREATE INDEX idx_minutes_owner ON minutes_jobs(owner_sub, created_at);
```

### 整形（generate）
- ステートレスに `stream_chat`（既定 `openai.gpt-oss-120b`、モデル選択可）を呼ぶ。会話履歴・短期メモリは使わない
- プロンプト: トランスクリプト（話者ラベル+タイムスタンプつき）+ テンプレート指示
  - `minutes`: 議事録（出席者(話者N)/決定事項/TODO(担当)/議論サマリ）
  - `faq`: Q&A形式の抜粋
  - `article`: 社内ニュース記事風
- トランスクリプトが長い場合は先頭から約24,000文字で打ち切り、その旨を出力に注記させる

### UI（/minutes、ナビの「議事録」を有効化）
- 左: アップロード（input file + 言語選択）+ ジョブ一覧（状態バッジ、クリックで選択、削除）
- 右: 選択ジョブのトランスクリプト（話者チップ+タイムスタンプ、話者ごとに色分け）
  - 完了前は5秒間隔でポーリング
- 下: テンプレート選択（議事録/FAQ/記事）+ モデル選択 → 生成（SSEストリーミング、Markdown表示、コピー）

### 完了条件
- ローカル（ユーザー認証）で日本語音声の E2E: アップロード→文字起こし（話者分離）→議事録生成
- 実環境はIAM追加後にE2E（それまで503ガード動作を確認）
- 検証レポート docs/verification/VOICE-01.md

## [VOICE-02] リアルタイム文字起こし画面

### 経路の決定（2026-06-12）
**API GatewayはWebSocket非対応**（公式: HTTP/Sのみ。
https://docs.oracle.com/en-us/iaas/Content/APIGateway/Concepts/apigatewayoverview.htm 。
GWデプロイメントのルート定義にもWSタイプが存在しない）→ クライアント⇔APIは
**音声=チャンクPOST / 結果=SSE** の中継方式を採用。比較は
`docs/comparison/realtime-transport.md`。WhisperリアルタイムにはpartialがないためWS双方向の利点が薄く、
SSEはSPIKE-02以降この経路で実証済み。API⇔OCIリアルタイムSTTは `oci-ai-speech-realtime`（WS、IAM署名）。

### API（セッションはAPIプロセス内に保持。Container Instance 1台構成が前提）
| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/stt/sessions` | `{language}` → OCIリアルタイムWS接続を確立し `{id}` を返す |
| POST | `/api/stt/sessions/{sid}/audio` | ボディ=16kHz mono PCM16生バイト列（最大64KB/回）を中継 |
| GET | `/api/stt/sessions/{sid}/events` | SSE: `{text, is_final}`（WHISPERはfinalのみ）+ `{closed}` |
| DELETE | `/api/stt/sessions/{sid}` | セッション終了（OCI側WSもclose） |

- セッション制約: 1ユーザー1本（新規作成で旧セッションをclose）、全体上限4、無操作120秒で自動close
- WHISPER制約（SPIKE-06）: `model_type="WHISPER"`、`encoding="audio/raw;rate=16000"`、
  partial関連パラメータは送らない
- 認証: 他APIと同じ `require_user`。署名はRP/ユーザー両対応（minutes._clientsと同パターン）

### UI（/realtime、ナビ追加）
- 開始/停止ボタン。getUserMedia→AudioContext(16kHz)→AudioWorkletでfloat32→int16変換、
  約250msごとにPOST
- 確定行をタイムスタンプ付きで追記表示、全文コピー。「発話の区切りごとに数秒遅れて確定」の注記
- マイクはHTTPS必須（GW経由は満たす。ローカルはlocalhost例外）

### 完了条件
- サーバーE2E: 16kHz WAVをHTTPチャンク送信→SSEでfinal受信（ローカル+GW経由）
- ブラウザ実機でマイク→文字起こし表示（人間確認でも可）
- 検証レポート docs/verification/VOICE-02.md

## [VOICE-03] 音声チャットv1

### 方式（半二重）
録音（トグル式: 話す→停止）→ **VOICE-02のリアルタイムSTTセッションを再利用**してfinal結合 →
`/api/chat/stream`（ステートレス、ページ内履歴を全送信）→ **TTSをセンテンス単位でパイプライン化**して順次再生。
再生中はマイクを開かない（半二重）。

### TTS API（新規）
| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/tts` | `{text(≤500字), voice}` → `audio/mpeg`（mp3バイト列） |

- `jetuse_core/tts.py`: Phoenixクロスリージョン（設定 `TTS_REGION`、既定 `us-phoenix-1`）。
  RP/ユーザー署名両対応。`compartment_id` 必須（無いと404 — VOICE-01のハマり）
- `TtsOracleTts2NaturalModelDetails(voice_id, language_code="ja-JP")` 明示（SPIKE-06ハマり）
- voiceはSPIKE-06で確認済みの日本語5種（Aiko/Hana/Sakura/Yuki/Satoshi）のallowlist。v1は日本語のみ

### クライアントのパイプライン
- 発話終了判定: 停止ボタン押下→無音1秒分(ゼロPCM)送信→最大5秒finalを待って結合。空なら注意表示
- LLM: system promptで「音声読み上げ前提。話し言葉・短文・記号/箇条書き回避」を指示
- センテンス分割: 蓄積deltaを `。．！？!?\n` で区切り、確定したセンテンスから順にTTSへ
  （fetchは先行発行=パイプライン、再生はHTMLAudioElementで順序保証）
- 停止ボタン: LLMストリーム中断+再生キュー破棄

### UI（/voicechat、ナビ追加）
- 会話バブル（ユーザー=STT結果、アシスタント=ストリーミングテキスト+読み上げ）
- モデル選択（既定gpt-oss-120b）・ボイス選択（既定Yuki）・自動読み上げのON/OFF
- 状態表示: 録音中 / 認識中 / 応答中 / 再生中

### 完了条件
- サーバーE2E: `/api/tts` がローカル+GW経由(RP・クロスリージョン)でmp3を返す
- 全体フローのブラウザ実機確認（マイク→応答→読み上げ）は人間確認項目
- 検証レポート docs/verification/VOICE-03.md

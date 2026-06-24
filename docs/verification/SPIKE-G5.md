# SPIKE-G5 (GAP-05): 音声チャットの全二重化 — OCIモデル有無の確定と到達可能UXの見極め

実施日: 2026-06-15 / リージョン: ap-osaka-1（TTSのみus-phoenix-1） / 判定軸: **マネージドで全二重が実現できるか**

## 目的

半二重（VOICE-03: 押して話す）を、JetUse の Amazon Nova Sonic 相当の **全二重**
（割り込み可・連続対話）へ近づけられるか。OCI に双方向音声(speech-to-speech)モデルがあるかの
go/no-go が肝。

## 結果1: 双方向音声(speech-to-speech)モデルの有無 — **無し（実機確定）**

`oci generative-ai model-collection list-models --region ap-osaka-1` の全モデル能力を再確認（2026-06-15）。
登録モデルの capabilities は **`CHAT` / `FINE_TUNE` / `TEXT_EMBEDDINGS` / `TEXT_RERANK`**（＋ガードレール系）のみ。

→ **`AUDIO` / speech-to-speech / realtime audio の能力を持つモデルは1つも存在しない。**
OpenAI互換APIの面（Responses / Conversations / Files / Vector Stores / File Search / Code Interpreter）にも
realtime audio / audio エンドポイントは無い（CLAUDE.md 確定事実）。

→ **Nova Sonic 相当の「単一モデルでの全二重音声対話」は OCI にマネージドで存在しない。**

## 結果2: STT/TTS パイプラインの全二重化の上限（SPIKE-06 で確定済みの制約）

speech-to-speech が無い以上、全二重は **リアルタイムSTT + LLM + ストリーミングTTS のパイプライン**で
擬似的に作るしかない。その上限は SPIKE-06 で確定済み:

| 制約 | 内容 | 全二重への影響 |
|---|---|---|
| リアルタイムSTTに **partial が無い** | WHISPERモードは final のみ。発話終了→数秒遅れで届く | 「話しながら認識」不可。ターン確定が数秒遅れる |
| TTS は **Phoenix往復**（+1.3s/文） | 大阪にTTS無し。クロスリージョン | 応答音声の出だしに遅延。センテンス分割で吸収可 |
| 真のバージイン（割り込み時のSTT/TTS同時処理）に必要な partial が無い | 上記と同根 | ユーザー発話途中での即時割り込み判定が弱い |

## ゲート判定: **真の全二重は no-go（マネージド軸・プラットフォーム制約）／到達目標を「連続半二重」に再定義**

- **真の全二重（speech-to-speech / 発話しながら相互割り込み）**: OCIにマネージドのモデルが無いため **no-go**。
  → A項目（プラットフォーム制約）へ降格して記録。
- **連続半二重（自動ターン検出＋バージイン）**: 既存マネージドSTT/TTSの上に**アプリ層（主にクライアント）**で
  到達可能。ただしこれは新しいマネージド能力の獲得ではなく **フロント実装の改善**:
  - クライアントVAD（無音検知）で発話終了を自動検出→自動送信（backlog #14）
  - 応答音声再生中にユーザー音声を検知したらTTS停止＝簡易バージイン
  - partial が無いため「相手が話し終わってから数秒で応答開始」が体感上限

## 推奨

判定軸（マネージドで導入できる分だけ）に従い、**真の全二重は未実装（OCIにマネージドモデルが無いため）**と記録。
連続半二重への改善（VAD自動送信＋簡易バージイン）は**任意のUX改善**として backlog に残す
（マネージド能力のギャップ解消ではなくフロント実装タスクのため、Bギャップの「解消」には数えない）。

## comparison/aws-reference.md への反映

GAP-05 は B（簡易版ギャップ）→ **A（プラットフォーム制約）**へ降格。
「全二重音声(Nova Sonic相当)は OCI に speech-to-speech モデルが無く未実装。
半二重(VOICE-03)で実現済み。連続半二重(VAD自動送信+簡易バージイン)はフロント改善として backlog」。

## 参照

- SPIKE-06（リアルタイムSTTにpartial無し / TTSはPhoenix往復 の実機確定）
- VOICE-02（リアルタイムSTT実装）/ VOICE-03（半二重音声チャット実装）
- モデル能力一覧（2026-06-15 実機: speech-to-speech 能力なし）

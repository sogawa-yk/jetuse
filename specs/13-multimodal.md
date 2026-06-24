# specs/13: MM-01 画像入力チャット・映像分析

## 実機確定事項（2026-06-12スパイク、大阪・OpenAI互換chat completions、画像はdata URI）

| モデル | 結果 |
|---|---|
| google.gemini-2.5-flash / pro | **画像入力OK**（content partsの `image_url`） |
| meta.llama-3.2-90b-vision-instruct | **画像入力OK** |
| cohere.command-a-vision | **404 Entity not found**（CPのモデル一覧には出るが互換APIのオンデマンド提供なし — Grok系と同パターン） |
| openai.gpt-oss-120b | リクエストは受理されるが**画像は見えない**（「画像を確認できません」と回答 — 受理≠機能の対照実験） |

→ 視覚対応はすべてchat completions系。Responses系(input_image)の対応は現状不要。

## 画像入力チャット

### API
- `ModelDef.vision: bool` を追加（gemini-2.5-pro/flash=true、`llama-3.2-90b-vision` を新規登録）。
  `/api/chat/models` に `vision` を含める
- `ChatRequest.images: list[str] | None`（data URI、**最大10枚(チャットUIは4枚)・各2MB・計10MB**）
  - 画像つきはvisionモデル必須(422)。agent/ragとの併用不可(422)。最終メッセージがuser必須
  - 適用: 最終userメッセージを `[{type:text},{type:image_url}...]` のcontent partsへ変換
  - 履歴・DB永続化はテキストのみ（画像は当該ターンのみ有効 — v1制約）

### UI（チャットページ）
- visionモデル選択時のみ📎ボタン表示。複数選択可（≤4）
- クライアントで長辺1024pxへ縮小しJPEG(q0.85)のdata URI化（帯域・トークン節約）
- 入力欄上にサムネイルチップ（削除可）。送信でクリア。ユーザー発話バブルにもサムネイル表示

## 映像分析（/video、ナビ追加）

- 動画ファイル（mp4等）をブラウザで読み、**等間隔Nフレーム（既定6・最大10）をcanvasで抽出**
  （サーバーへ動画は送らない — 軽量・プライバシー面でも有利）
- フレームサムネイル一覧 + プロンプト（既定「この映像で何が起きているか…」）+ visionモデル選択
- 実行 = `/api/chat/stream` に images=フレーム群 でステートレス送信 → Markdown表示
  （サーバー側は画像入力チャットの機構をそのまま再利用。専用APIなし）

## 完了条件
- 画像チャット: 実画像でgemini/llama両モデルE2E（ローカル+実環境）
- 映像分析: サンプル動画でフレーム抽出→分析E2E
- 検証レポート docs/verification/MM-01.md

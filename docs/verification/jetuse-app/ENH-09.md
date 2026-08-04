# ENH-09: 映像分析機能のエラー修正

実施日: 2026-06-15

## 原因(実機特定)
映像分析は動画を等間隔Nフレーム(既定6)に分割し vision モデルへ一括送信する。
モデルに **meta.llama-3.2-90b-vision** を選ぶと `400 "At most 1 image(s) may be provided
in one request"` で失敗(複数画像非対応)。gemini-2.5-pro/flash は6枚でも正常(実機確認)。

## 修正
- `ModelDef.multi_image` フラグを追加(gemini-2.5-pro/flash=True、llama-3.2-vision=False)。
  `/api/chat/models` で公開。
- video.tsx: モデル候補を `vision && multi_image` に限定(複数フレーム送信のため)。
- chat.tsx: 単一画像モデル選択時は添付を1枚に制限(`maxImages`)。

## 検証
- OCI直接呼び出し: gemini-2.5-flash は6画像でOK、llama-3.2-90b-vision は400(上記メッセージ)。
- build/lint/ruff グリーン。API 0.36.0 / SPA デプロイ。

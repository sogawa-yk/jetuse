# 実機切り分け（2026-08-03・ユーザープリンシパルで直接 OCI Speech を呼んだ結果）

同一パラメータ（TTS_2_NATURAL / voice=Yuki / ja-JP / MP3 / compartment=jetuse:test）:

| region | list_voices | synthesize_speech |
|---|---|---|
| us-phoenix-1 | OK（ja-JP 5ボイス・allowlist 一致5） | **OK (6783 bytes)** |
| us-chicago-1 | **500 `Connection refused: <OCI_INTERNAL_ENDPOINT>`** | 到達せず |
| us-ashburn-1 | OK | OK (6783 bytes) |

→ Phoenix は正常。シカゴのみ OCI 側の内部障害。

## 配備構成（なぜ 503 になったか）
- `/api/tts` は **Functions ルーター**が処理（`fn_router_segments = ["presets","dbchat","tts"]`）
- Functions プロセスは `_resolved_region=None` → 候補は `["us-chicago-1","us-phoenix-1"]`
- 修正前は 401/403/404 のときだけ次リージョンへ回るため、シカゴの 500 で打ち切り
- `/api/health` は Container Instance が処理し、プローブが Phoenix を掴むので `verified:true` を返す
  → 「health は緑なのに /api/tts は 503」という食い違い

## この文書の位置づけと、検証範囲

本文書は**修正前**の切り分け記録である（どのリージョンが壊れているかの特定）。

修正後の裏づけは別文書に分けている:
- `real-service-before-after.md` — 実 OCI Speech に対する修正前後の対比（実施済み）
- `SKIPPED.md` — 配備済みスタックの API Gateway → Functions 経路（**未実施**。
  コンテナ画像の再ビルドがゲートで、それは main への merge 時に release.yml が行うため）

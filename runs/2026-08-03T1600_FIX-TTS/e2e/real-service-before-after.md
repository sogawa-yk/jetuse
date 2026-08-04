# 実 OCI サービスに対する修正前後の対比（モックなし）

`jetuse_core.tts.synthesize()` を、**配備時の Functions プロセスと同じ状態**から実行した。
- `_resolved_region = None`（プロセス起動直後＝解決済みリージョンを持たない）
- `OCI_REGION=us-chicago-1`（デプロイリージョン） / `TTS_REGION` 未指定
- `COMPARTMENT_OCID` = jetuse:test / `AUTH_MODE=config_file`
- 候補リージョンはどちらも `['us-chicago-1', 'us-phoenix-1']`

| | 読み込んだ実装 | 結果 |
|---|---|---|
| 修正前 | `.../jetuse/packages/api/jetuse_core/tts.py` | **FAIL** `TtsError: 音声合成に失敗しました: InternalError Internal Server Error` |
| 修正後 | `.../jetuse-loops/FIX-TTS/packages/api/jetuse_core/tts.py` | **OK** 18015 bytes / 先頭 `b'ID3'`（MP3）/ 成功リージョン `us-phoenix-1` |

修正後のログ:

```
TTS unavailable in us-chicago-1 (HTTP 500 InternalError); trying next region
```

→ シカゴの 5xx で打ち切らず Phoenix へフォールバックし、実際に音声が返ることを実サービスで確認した。

## この証跡が示せること / 示せないこと

- **示せる**: 修正した分岐が、実際に 500 を返しているリージョンに対して意図どおり働く。
  返り値が本物の MP3 であること（ID3 ヘッダ）。
- **示せない**: 配備済みスタックの **API Gateway → Functions → /api/tts** の経路。
  これは `SKIPPED.md` のとおり画像の再ビルドがゲートになっている。

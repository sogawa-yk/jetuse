# シナリオ1: 修正版を配備し、`POST /api/tts` が実際に音声を返すことを確認する（実施済み）

`SKIPPED.md` に「merge 後に実施が要る」と記した確認を、PR #124 の merge 後に実施した。

## 手順（実施）

1. PR #124 を main へ merge（merge commit `60aefaf`）
2. release.yml が画像を生成（成功）。シカゴの OCIR に
   `jetuse-api` / `jetuse-fn-router` / `jetuse-agent-{openai,langgraph,adk}` の
   タグ `60aefaf...` が揃っていることを確認
3. 配布 ZIP を取得し、`image_tag` の既定値が `60aefaf...` に固定されていることを確認
4. スタック `jetuse-pub`（jetuse:test / us-chicago-1）の config source をその ZIP へ差し替え
5. plan で影響範囲を確認 → apply

## plan / apply の結果

```
Plan:  131 to add, 5 to change, 131 to destroy
Apply: 131 added, 5 changed, 131 destroyed
```

置換されたのは **SPA ファイル（ビルドハッシュ変更）・Container Instance・エージェント3種**のみ。
**ADB / バケット / VCN / Identity Domain は置換・削除 0 件**（plan で事前確認）。
アプリの URL も変わっていない。

## 受け入れ E2E（修正後）

| スイート | 修正前 | 修正後 |
|---|---|---|
| `public-deploy.mjs` | 38 / 39 | **39 / 39 PASS** |
| `agents-3sdk.mjs` | 9 / 9 | **9 / 9 PASS** |
| 4xx/5xx レスポンス | `503 POST /api/tts` 1件 | **0件** |

TTS の該当項目:

```
PASS  TTS: 音声合成
      — 200 ID3 ... ORACLE TSSE ... Lavf58.76.100 ...
PASS  TTS: health が実合成の結果を反映(verified)
      — {"status":"ok","region":"us-phoenix-1","candidate_regions":["us-phoenix-1","us-chicago-1"],"verified":true}
```

**`POST /api/tts` が 200 で ID3 ヘッダ付きの MP3（ORACLE TSSE エンコーダ）を返した。**
配備経路（API Gateway → Functions）でシカゴの 5xx から Phoenix へフォールバックし、
実際に音声が生成されている。health と実合成の食い違いも解消した。

エージェントの初回応答は 2.6〜2.9 秒（配備直後のコールドスタート込み）。

## 結論

`SKIPPED.md` に残していた唯一の未実施項目は解消した。本修正は**実環境の配備経路で確認済み**。

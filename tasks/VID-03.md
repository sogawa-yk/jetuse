# タスク: VID-03 AI 分析（視覚 LLM）

## 目的
場面ごとのメタデータと映像全体の要約を AI が付ける。

**2026-08-20 の実測で構成を改訂した（ADR-0032 決定1）。** OCI AI Vision の `video-job` は
大阪・シカゴとも 404 で**機能が無い**（同じ資格情報で `analyze-image` は成功するので権限ではない）。
また AI Vision の `TEXT_DETECTION` は**日本語テロップを読めない**（`大阪` → `XRR`。gemini は正確）。
要求13 は日本語が主対象なので、**内容の抽出は視覚 LLM に一本化する。**

## 仕様参照: specs/20-video-search.md §3（3〜6）/ ADR-0032 決定1・決定5

## 前提
- VID-02（場面と代表フレームがある）

## 作業内容
- `jetuse_core/video_analyze.py`
  - **記述層**: 視覚 LLM（既定 `gemini-2.5-pro`）へ区間のフレームを渡し、**構造化 JSON** で受ける
    （説明・タグ・物体・人物・場所・種別・屋内外・時間帯・天候・行動・**画面内文字**）。
    **判らない項目は `unknown` を返させる。時刻は聞かない**（区間は ffmpeg 側が持っている）
  - **AI Vision は使わない**（上記の実測）。`vision_state` 列は残し常に `skipped` を入れて
    「この経路を通っていない」ことを記録する（列を消すと後から経緯が辿れない）
  - 映像全体の要約（要求9）
  - 埋め込み（`cohere.embed-multilingual-v3.0`）を場面ごとに作る
- `POST /api/video/assets/{id}/analyze`（再分析も同じ入口）
- 状態遷移 `pending`→`running`→`done`/`partial`/`failed`。**失敗理由を必ず残す**
- **IAM の追加は不要**（AI Vision を使わないため。当初は `ai-service-vision-family` が要った）

## 完了条件
- 実 OCI で映像1本を分析し、場面ごとに説明・タグ・物体が入ること
- **日本語のテロップが読めること**を実測で示す（要求13。`大阪` 等の地名が正しく取れること）
- 判らない項目が `unknown` になり、**もっともらしい値で埋まらない**ことを実測で示す
- 単体テスト: LLM が JSON を返さない/欠損したときの扱い、状態遷移、`unknown` の保持

## 成果物
コード / `docs/verification/VID-01.md` に追記

## 前タスクの残（VID-02 の Codex major。あなたの範囲なので取り込む）
- `jetuse_core/video.py` の `finish_analysis` は「`failed` なら理由を必ず入れる」と書いてあるのに
  **`state='failed', error=None` を保存できる**。state 文字列の検証も無い。**このタスクが
  この関数の主な利用者**なので、値を検証して不正な組み合わせを弾くこと（理由の無い失敗を作らない）。
- `ops` 系の `verify_migrations.py` は、使い捨てスキーマの作成後に例外で抜けると
  **`DROP USER` に到達せず、CREATE 権限と無制限 quota を持つ検証ユーザーが残る**。
  `try/finally` で後始末を保証すること（実 DB にユーザーが溜まる）。
- `video_frames.py` の `_invoke` は `FileNotFoundError` とタイムアウトしか変換せず、
  実行権限が無い場合の `PermissionError` 等が生の例外で漏れる。`FfmpegUnavailableError` に寄せること。

## 禁止事項
- LLM に時刻や、渡していない情報を答えさせること
- 判らない項目をもっともらしい値で埋めること（`unknown` にする）
- AI Vision を使う実装を足すこと（実測で不採用。必要になったら ADR を改訂してから）

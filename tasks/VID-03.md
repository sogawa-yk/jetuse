# タスク: VID-03 AI 分析（視覚 LLM + OCI AI Vision の2層）

## 目的
場面ごとのメタデータと映像全体の要約を AI が付ける。構造化（物体・文字・区間）と記述（自然言語）を
役割で分ける。**AI Vision が使えなくても縮退して成立させる。**

## 仕様参照: specs/20-video-search.md §3（3〜6）/ ADR-0032 決定1・決定5

## 前提
- VID-02（場面と代表フレームがある）

## 作業内容
- `jetuse_core/video_analyze.py`
  - **記述層**: 視覚 LLM（既定 `gemini-2.5-pro`）へ区間のフレームを渡し、**構造化 JSON** で受ける
    （説明・タグ・物体・人物・場所・種別・屋内外・時間帯・天候・行動・画面文字）。
    **判らない項目は `unknown` を返させる。時刻は聞かない**
  - **構造化層**: OCI AI Vision `video-job`（LABEL / OBJECT / TEXT_DETECTION）。区間つきの結果を
    場面へ突き合わせる。**受理されない・権限が無い場合は `vision_state=skipped` で続行**
  - 映像全体の要約（要求9）
  - 埋め込み（`cohere.embed-multilingual-v3.0`）を場面ごとに作る
- `POST /api/video/assets/{id}/analyze`（再分析も同じ入口）
- 状態遷移 `pending`→`running`→`done`/`partial`/`failed`。**失敗理由を必ず残す**
- IAM: AI Vision を使う場合の動的グループ・ポリシー追加（**apply は人間の承認ゲート**）

## 完了条件
- 実 OCI で映像1本を分析し、場面ごとに説明・タグ・物体が入ること
- **AI Vision を意図的に無効にした状態でも `done`（`vision_state=skipped`）で完了する**ことを実測
- 判らない項目が `unknown` になり、**もっともらしい値で埋まらない**ことを実測で示す
- 単体テスト: LLM が JSON を返さない/欠損したときの扱い、Vision 失敗時の縮退、状態遷移

## 成果物
コード / `docs/verification/VID-01.md` に追記 / AI Vision の可用性の実測結果（比較ドキュメントへ反映）

## 禁止事項
- Vision が使えないことを「問題なし」に丸めること
- LLM に時刻や、渡していない情報を答えさせること

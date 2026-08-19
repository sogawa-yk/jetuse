# タスク: VID-05 メタデータの確認・修正（出所の区別）

## 目的
AI の結果をそのまま確定情報として扱わず、人が確認・修正できるようにする。

## 仕様参照: specs/20-video-search.md §5 / ADR-0032 決定5

## 前提
- VID-03（場面メタデータがある）

## 作業内容
- `PATCH /api/video/scenes/{id}` — 説明・タグ・文字・場所・カテゴリの修正。**`source` を `human` に**
- `POST /api/video/scenes/{id}/confirm` — 確認済み（`source` を `ai_confirmed` に）
- `DELETE /api/video/scenes/{id}` — 不適切なメタデータの削除
- **修正したらその場面の埋め込みを作り直す**（直したのに検索結果が変わらないのは筋が通らない）
- `VIDEO_SCENE_EDITS` に変更履歴を残す

## 完了条件
- 修正 → 再検索で結果が変わることを実測で示す
- `ai` / `human` / `ai_confirmed` が API から区別できること
- 単体テスト: 出所の遷移、履歴の記録、埋め込み再生成、権限（他人の場面を直せない）

## 成果物
コード / `docs/verification/VID-01.md` に追記

## 禁止事項
- 修正後に埋め込みを作り直さないこと
- 出所を上書きして判らなくすること

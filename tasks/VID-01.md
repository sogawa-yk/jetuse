# タスク: VID-01 映像の保管と登録（データモデル + Object Storage）

## 目的
映像を JetUse に登録して一覧・詳細・削除でき、Object Storage 上の本体を期限付き URL で
再生できるようにする。以降のタスク（分析・検索・UI）が乗る土台。

## 仕様参照: specs/20-video-search.md §1 §2

## 前提（依存タスク / 人間の事前作業）
- 依存なし（このステージの第1波）
- infra に映像用バケットを足す。**terraform apply は人間の承認ゲート**

## 作業内容
- migration `022_video_assets.sql` / `023_video_scenes.sql`（Public 帯 `0xx_`）
  - `VIDEO_ASSETS` / `VIDEO_SCENES` / `VIDEO_SCENE_EDITS`。`VECTOR` 列は 23ai
  - **NULL と `unknown` を区別する**（未分析 / 分析したが判らなかった）
- `jetuse_core/video.py`: 登録・一覧・詳細・削除、Object Storage への put/delete、PAR 発行
- `service/routes/video.py`: `POST/GET/DELETE /api/video/assets`、`GET .../playback`
- infra: 映像バケット（`modules/object-storage` に追加）
- 所有者分離は既存の `owner_sub` に合わせる

## 完了条件（検証可能な形で）
- 実 OCI（`jetuse:public-dev`）で映像を登録 → 一覧に出る → `playback` の URL でブラウザ再生できる
- 削除で Object Storage 上の本体も消えること（残骸を残さない）
- 単体テスト: 登録・一覧・削除・PAR 期限・所有者分離
- `/api/health` の `schema` が `behind` にならないこと（migration が流れている）

## 成果物
コード / `docs/verification/VID-01.md`

## 禁止事項
- 認証情報・実 OCID のコミット
- `jetuse-spike-` 以外のリソース削除
- 映像本体を DB に入れること（Object Storage に置く）

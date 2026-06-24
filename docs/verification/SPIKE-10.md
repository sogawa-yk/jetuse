# SPIKE-10: Enterprise AI 長期メモリ（subject_id）API探索

実施日: 2026-06-11 / 対象: ap-osaka-1 OpenAI互換エンドポイント / 目的: AGT-05の前提確認

## 結論: **subject_idベースの長期メモリAPIは現行エンドポイントに存在しない（未公開）**

## 探索結果

| 探索 | 結果 |
|---|---|
| `GET /memories` `/long_term_memories` `/subjects` `/users` `/memory` | すべて404（"Path doesn't map to a registered service!"） |
| `responses.create(user=...)` | 400 "Field 'user' is not supported"（既知フィールドは明示拒否される） |
| `responses.create(extra_body={subject_id, long_term_memory})` | 受理されるが**機能しない**（書き込み後5/20/40秒待っても別文脈で想起されず「不明」） |
| 会話に紐づく抽出（conversation+subject_id→30秒後に新規文脈で想起） | 不成立 |
| **偽陽性の確認**: `extra_body={"memory": {}}` 等のデタラメなフィールド | **すべて受理される** = extra_bodyの未知フィールドは黙殺される仕様。「受理」は機能の証拠にならない |

## 教訓（tips.mdにも記録）

OpenAI互換APIのフィールド探索では、**OpenAI本家に存在するフィールド（user等）は明示拒否されるが、完全に未知のフィールドは黙って無視される**。受理＝対応と誤認しないこと（デタラメフィールドでの対照実験が必須）。

## AGT-05への影響

計画の「Enterprise AI Agentsの長期メモリ統合（subject_id）」はプラットフォーム機能として利用不可。代替方針は **ADR-0006** を参照（人間レビュー待ち）。

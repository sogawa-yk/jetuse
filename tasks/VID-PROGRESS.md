# VID 映像の場面検索 進捗キュー（stage-runner の単一の真実源）

映像を登録すると AI が場面単位で分析してメタデータを付与し、利用者が自然言語・条件で複数映像を
横断検索して**該当場面から直接再生**できるようにする（2026-08-19 の要求）。

- **base = `public-dev`**（共有機能。ADR-0028。マージ後に internal へ同期する）
- 仕様 `specs/20-video-search.md` / 判断 `docs/decisions/ADR-0032-video-scene-search.md` /
  比較 `docs/comparison/video-scene-analysis-on-oci.md`
- **E2E 環境**: `jetuse:public-dev`（`make where` で版と配備先を確認してから着手する）
- **人間ゲート**: コミット / PR / push、terraform apply、**IAM（動的グループ・ポリシー）変更**

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | [VID-01 映像の保管と登録](VID-01.md) | — | バケット追加の apply（**済 2026-08-19**）+ コミット | done |
| 2 | [VID-02 場面分割とフレーム抽出](VID-02.md) | VID-01 | コミット | done |
| 3 | [VID-03 AI 分析（視覚 LLM）](VID-03.md) | VID-02 | コミット | done |
| 4 | [VID-04 場面の横断検索](VID-04.md) | VID-03 | コミット | done |
| 5 | [VID-05 メタデータの確認・修正](VID-05.md) | VID-03 | コミット | done |
| 6 | [VID-06 UI](VID-06.md) | VID-04, VID-05 | コミット | done |
| 7 | [VID-07 大きな映像の登録](VID-07.md) | VID-06 | コミット | todo |

> VID-04 と VID-05 は相互独立（検索は読み、編集は書き。ファイル衝突が無い）ので並列可。
>
> **AI Vision `video-job` の可用性は未検証。** VID-03 で実測する。受理されない場合は
> `vision_state=skipped` で縮退し、**ステージは止めない**（物体・画面内文字が空になるだけで、
> 場面説明・要約・ベクトル検索は成立する）。実測の結果は比較ドキュメントへ反映する。
>
> **2026-08-20 追加**: 配備して触ったところ **API Gateway の本体サイズ上限で 4K 映像が登録できなかった**
> （17MB は通り 20MB で 413）。各タスクの E2E は Object Storage を SDK で直接叩いており、
> **ゲートウェイを経由する経路を通していなかった**。VID-07 で直接アップロードへ変える。
>
> v1 の外: 音声の文字起こし（要求12）/ CSV 出力・外部連携（要求14）/ 長時間・大量映像の一括処理。

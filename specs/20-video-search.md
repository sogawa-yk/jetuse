# specs/20: VID-01 映像の場面検索・メタデータ付与

要求を一文で表すと:

> 登録された映像を AI が**場面単位**で分析し、内容・物体・人物・行動・表示文字などのメタデータを
> 自動付与する。利用者は自然言語および各種条件によって**複数映像を横断検索**し、検索条件に該当した
> **場面を直接確認**できる。また、AI が付与した情報は人が確認・修正・再利用できるものとする。

判断の根拠は [ADR-0032](../docs/decisions/ADR-0032-video-scene-search.md)、
方式比較は [docs/comparison/video-scene-analysis-on-oci.md](../docs/comparison/video-scene-analysis-on-oci.md)。

**用途を放送素材に限定しない。** 設備映像・記録映像・過去事例の検索にも使える汎用機能として作る。
語彙は「番組名」ではなく **`collection`（所属）**、「素材」ではなく **`asset`（映像）** を使う。

## 0. 全体像

```
登録 ─→ Object Storage（映像本体・サムネイル）
         │
         ├─→ ffmpeg  : 場面転換検出 → 区間 → 代表フレーム
         │      └─→ 視覚 LLM : 場面説明・行動・天候・時間帯・屋内外・出来事
         │
         └─→ AI Vision video-job : ラベル / 物体 / 画面内文字（区間つき）※使えなければ空で縮退
                        ↓
               場面メタデータ ＋ 埋め込み（cohere multilingual v3）
                        ↓
              ADB 23ai（ベクトル距離と条件を 1 クエリで）
                        ↓
    検索 → 場面（開始/終了・サムネイル・説明・一致理由）→ その時刻から再生
```

## 1. データモデル（migration `022_video.sql`〜、Public 帯）

### `VIDEO_ASSETS` — 映像

| 列 | 型 | 備考 |
|---|---|---|
| `id` | `VARCHAR2(64)` PK | |
| `owner_sub` | `VARCHAR2(255)` | 既存の所有者分離に合わせる |
| `title` | `VARCHAR2(500)` | 要求1 |
| `summary` | `CLOB` | 映像全体の要約（要求9・AI 生成） |
| `captured_at` | `TIMESTAMP` | 撮影日時。**不明なら NULL**（推測で埋めない） |
| `created_at` | `TIMESTAMP` | 登録日時 |
| `duration_ms` | `NUMBER` | 映像の長さ |
| `collection` | `VARCHAR2(255)` | 番組名・案件名などの所属 |
| `category` | `VARCHAR2(255)` | |
| `rights` | `VARCHAR2(1000)` | 権利・利用範囲（要求1） |
| `object_name` | `VARCHAR2(1024)` | Object Storage 上の位置 |
| `thumb_object` | `VARCHAR2(1024)` | 代表サムネイル |
| `analysis_state` | `VARCHAR2(32)` | `pending` / `running` / `done` / `failed` / `partial` |
| `analysis_error` | `VARCHAR2(4000)` | 失敗の理由。**空にしない** |
| `vision_state` | `VARCHAR2(32)` | AI Vision 層の状態。`skipped` を含む（縮退したことを残す） |

### `VIDEO_SCENES` — 場面（この機能の主役）

| 列 | 型 | 備考 |
|---|---|---|
| `id` | `VARCHAR2(64)` PK | |
| `asset_id` | `VARCHAR2(64)` FK | |
| `start_ms` / `end_ms` | `NUMBER` | **実測から作る。LLM に答えさせない** |
| `description` | `CLOB` | 場面説明（要求3） |
| `tags` | `CLOB` (JSON) | キーワード・タグ |
| `objects` | `CLOB` (JSON) | 物体 |
| `people` | `CLOB` (JSON) | 人物（有無・人数） |
| `place` | `VARCHAR2(255)` | 場所 |
| `scene_kind` | `VARCHAR2(64)` | スタジオ / 屋外 / 道路 / 建物内 など |
| `indoor` | `VARCHAR2(16)` | `indoor` / `outdoor` / `unknown` |
| `time_of_day` | `VARCHAR2(16)` | `day` / `night` / `unknown` |
| `weather` | `VARCHAR2(64)` | 不明なら `unknown` |
| `actions` | `CLOB` (JSON) | 行動・出来事 |
| `screen_text` | `CLOB` | 画面内の文字 |
| `thumb_object` | `VARCHAR2(1024)` | 場面のサムネイル |
| `source` | `VARCHAR2(16)` | **`ai` / `human` / `ai_confirmed`**（ADR-0032 決定5） |
| `confirmed_at` | `TIMESTAMP` | 人が確認した時刻。未確認は NULL |
| `embedding` | `VECTOR` | 23ai。検索用 |

**`unknown` は文字列として持つ。** NULL と `unknown` を区別する —— NULL は「まだ分析していない」、
`unknown` は「分析したが判らなかった」。要求2 の「認識できなかった項目は無理に埋めず、不明として
扱える」を満たすには、この2つを混ぜてはいけない。

### `VIDEO_SCENE_EDITS` — 修正履歴

人が何を直したかを残す（要求8）。`scene_id` / `field` / `before` / `after` / `edited_by` / `edited_at`。

## 2. 登録（要求1）

- `POST /api/video/assets` — multipart で映像を受け、Object Storage へ置き、`pending` で登録
- **複数件をまとめて登録できる。** 1リクエスト1本だが、UI は複数ファイルを選んで順に投げる
- `GET /api/video/assets` — 一覧（条件・ページング）
- `GET /api/video/assets/{id}` — 詳細（場面つき）
- `DELETE /api/video/assets/{id}` — 映像・サムネイル・場面をまとめて削除

再生は Object Storage の PAR で行う（`GET /api/video/assets/{id}/playback` が期限付き URL を返す）。

## 3. 分析（要求2・3・9）

`POST /api/video/assets/{id}/analyze`（再分析も同じ入口。要求8 の「再分析の依頼」）。

1. **場面分割** — `ffmpeg` の場面転換検出。短すぎる区間は併合、長すぎる区間は分割
2. **代表フレーム** — 各区間から数枚。サムネイルは Object Storage へ
3. **記述** — 視覚 LLM（`gemini-2.5-pro` 既定）に区間のフレームを渡し、**構造化 JSON** で受ける。
   判らない項目は `unknown` を返させる。**時刻は聞かない**
4. **構造化** — AI Vision `video-job`。ラベル・物体・画面内文字を区間つきで取り、場面へ突き合わせる。
   **使えなければ `vision_state=skipped` にして続行**（物体・文字が空になるだけ）
5. **要約** — 場面説明をまとめて映像全体の要約を作る（要求9）
6. **埋め込み** — 場面説明＋タグ＋文字を `cohere.embed-multilingual-v3.0` でベクトル化

**失敗を握りつぶさない。** 途中で落ちたら `failed` と理由を残す。一部だけ成功したら `partial`。
「分析済み」と「分析したが取れなかった」を同じ表示にしない。

## 4. 検索（要求4・5・10・11）

`POST /api/video/search`

```jsonc
{
  "q": "雨の中でリポーターが話している場面",   // 自然言語（任意）
  "filters": {                                  // 条件（任意・要求5）
    "captured_from": "2026-01-01", "captured_to": "2026-12-31",
    "collection": "...", "category": "...", "place": "...",
    "indoor": "outdoor", "time_of_day": "night",
    "has_people": true, "tags": ["雨"],
    "duration_min_ms": 0, "duration_max_ms": 600000,
    "analysis_state": "done", "confirmed": false, "rights": "..."
  },
  "similar_to_scene_id": null,                  // 類似検索（要求10）
  "limit": 20
}
```

- `q` を埋め込み、**ベクトル距離と条件を同一の SQL** で評価する（ADR-0032 決定4）
- `q` が無ければ条件だけの絞り込み（要求5 の「一覧から条件を選択して探せる」）
- `similar_to_scene_id` があればその場面のベクトルで再検索（要求10）
- 返すのは**場面**。映像ではない（要求6）

```jsonc
{ "hits": [{
    "scene_id": "...", "asset_id": "...", "title": "...",
    "start_ms": 73000, "end_ms": 91000,
    "thumb_url": "https://...",          // PAR
    "description": "傘を差した人物が濡れた路面の前で話している",
    "matched": {                          // 根拠（要求11）
      "reason": "「雨天」「屋外」「人物が話している」が検索条件と一致しています",
      "fields": ["weather", "indoor", "actions"],
      "tags": ["雨", "屋外"],
      "distance": 0.18
    },
    "asset": { "collection": "...", "captured_at": "...", "duration_ms": 0 }
}] }
```

**根拠は必ず返す。** なぜその場面が出たのかを利用者が確認できることは要求11 の要件であり、
**AI 検索をブラックボックスにしない**という設計方針そのもの。

## 5. 編集（要求8）

- `PATCH /api/video/scenes/{id}` — 説明・タグ・文字・場所・カテゴリの修正。**`source` を `human` に**
- `POST /api/video/scenes/{id}/confirm` — 確認済みにする（`source` を `ai_confirmed` に）
- `DELETE /api/video/scenes/{id}` — 不適切なメタデータの削除
- 修正すると**その場面の埋め込みを作り直す**（直したのに検索結果が変わらないのは筋が通らない）
- 変更は `VIDEO_SCENE_EDITS` に残す

## 6. UI

| 画面 | 内容 |
|---|---|
| `/videos` | 一覧・登録（複数選択可）・分析状態・条件絞り込み |
| `/videos/search` | 自然言語入力＋条件パネル。結果は**場面カード**（サムネイル・時刻・説明・一致理由） |
| `/videos/{id}` | プレーヤ＋**タイムライン**（場面帯・種別で色分け）。帯を選ぶとその時刻へ移動（要求7）。場面ごとの編集・確認・再分析 |

検索結果を選ぶと `/videos/{id}?t=73.0` で開き、**その時刻から再生**する（要求6）。
利用者が映像全体を最初から確認しなくても目的の場面へ直接移動できることが、この機能の主要な価値。

## 7. v1 の外

| # | 要求 | 扱い |
|---|---|---|
| 12 | 音声の文字起こしと発話検索 | 後続。OCI Speech（`specs/12`）。要求側でも「段階的な拡張機能」と整理済み |
| 14 | CSV 出力・他システム連携 | JSON 取得までは v1。CSV と外部連携は後続 |
| — | 長時間・大量映像の一括処理 | まず短い映像で成立させ、規模は測ってから |

## 8. 完了条件

- 実 OCI（`jetuse:public-dev`）で、複数の映像を登録 → 分析 → 自然言語検索 → 該当時刻から再生
  → メタデータ修正 → 再検索、まで通ること
- AI Vision が使えない場合に**縮退して成立する**ことを実際に確かめる（`vision_state=skipped`）
- 検証レポート `docs/verification/VID-01.md`

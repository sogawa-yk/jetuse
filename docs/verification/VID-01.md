# VID-01 検証レポート: 映像の保管と登録（データモデル + Object Storage）

実施日: 2026-08-19 / リージョン: us-chicago-1（Object Storage）/ ap-osaka-1（ADB）/ 対象: `jetuse_core/video.py` + `service/routes/video.py` + migration 022/023
仕様の正本: `specs/20-video-search.md` §1 §2 / 判断: `docs/decisions/ADR-0032-video-scene-search.md`
証跡: `runs/2026-08-19T2225_VID-01/e2e/`

## 結論（先に）

映像を登録して一覧・詳細・削除でき、Object Storage の本体を期限付き URL（PAR）で
**実ブラウザで再生できる**ところまでを実 OCI で確認した。削除は本体・サムネイル・PAR・
場面行のすべてを残さない。

映像用バケットの `terraform apply` は**人間の承認を得て実行された**（2026-08-19・ORM 経由で
`jetuse-pubdev-video` を作成）。**E2E はその正規バケットに対してやり直した**。
残る未達は「public-dev に配備済みの API コンテナ経由での疎通」だけで、これは
`runs/2026-08-19T2225_VID-01/e2e/SKIPPED.md` に理由を明記している。

| 完了条件 | 結果 | 証跡 |
|---|---|---|
| 登録 → 一覧 → 詳細 | ○ 実 Object Storage + 実 ADB。**実 HTTP でも**確認 | `e2e/scenario-1.txt` / `scenario-7.txt` |
| `/api/health` の `schema` が `behind` でない | ○ `{"status":"ok","applied":23,"expected":23}` | `e2e/scenario-7.txt` |
| `playback` の URL でブラウザ再生 | ○ Chromium で `readyState=4` / 再生進行を確認 | `e2e/scenario-2.txt` / `e2e/scenario-6.txt` / `e2e/screenshots/scenario-6-browser-playback.png` |
| 削除で Object Storage の本体も消える | ○ 本体・サムネイル・PAR・場面がすべて 0 | `e2e/scenario-5.txt` |
| 所有者分離 | ○ 他人の映像は「存在しない」扱い | `e2e/scenario-3.txt` |
| NULL と `unknown` の区別 | ○ 同じ列で別の値として残る | `e2e/scenario-4.txt` |
| 単体テスト / `make lint` / `make test` | ○ `test_video.py` 18 件・全体パス | — |
| 映像用バケットの apply | ○ 人間の承認後に実行済み（`jetuse-pubdev-video`） | `e2e/terraform-plan.txt` |
| 配備済み API コンテナ経由の疎通 | **未実施**（配備自体が後続の人間ゲート） | `e2e/SKIPPED.md` |

## 1. データモデル（migration 022 / 023）

実 ADB（23ai / `jetuse-loop-adb`）へ適用済み。`applied: ['022_video_assets', '023_video_scenes']`。

| 表 | 役割 |
|---|---|
| `VIDEO_ASSETS` | 映像の台帳。**本体は入れず `object_name` だけを持つ** |
| `VIDEO_SCENES` | 場面。`embedding VECTOR(1024, FLOAT32)`（cohere multilingual v3 と同次元） |
| `VIDEO_SCENE_EDITS` | 人が直した履歴（要求8） |

### 決めたこと 1: NULL と `unknown` を混ぜない

仕様の要である「まだ分析していない（NULL）」と「分析したが判らなかった（`unknown`）」の区別を、
**CHECK 制約が NULL を通す**性質で表現した。`indoor` / `time_of_day` は
`CHECK (indoor IN ('indoor','outdoor','unknown'))` を持つが、NULL はこの制約に掛からない。
**既定値は入れない** —— 既定で `'unknown'` を埋めると、未分析と区別できなくなる。

実機で両方を入れて確認した（`scenario-4.txt`）:

```
('5df4b423-...', None, None, None)                    ← 未分析
('49811f42-...', 'unknown', 'unknown', 'unknown')     ← 分析したが判らなかった
```

`vision_state` も同じ形にした。NULL = AI Vision 層に触れていない / `'skipped'` = 触れたが
使えないので縮退した（ADR-0032 決定1 の「縮退したことを残す」）。

### 決めたこと 2: JSON 列は `IS JSON` で守る

`tags` / `objects` / `people` / `actions` は CLOB(JSON)。後続タスクが `JSON_VALUE` で
検索するので、壊れた値が入ると検索時まで気付けない。入口で弾く:

```
ORA-02290: check constraint (JETUSE_MNPDEMO.VIDEO_SCENES_TAGS_CK) violated
```

### 決めたこと 3: 文字列列は CHAR セマンティクス

`title VARCHAR2(500 CHAR)` のように日本語を文字数で扱う。BYTE セマンティクスのままだと
日本語 1 文字 3 バイトで ORA-12899 になる（`rag_adb.doc_key` が同じ問題を回避している）。
`analysis_error` だけは 4000 **バイト**のまま（4000 CHAR は拡張文字列設定に依存するため）。
列幅に収める切り詰めは `_fit` の 1 箇所だけで行い、**保存する値と返す値を同じにする**
（別々に作ると POST の応答と直後の GET が食い違う）。空文字は `None`（値なし）に寄せる。

### 仕様からの逸脱（1 点）

`VIDEO_SCENE_EDITS` の列名は仕様の `before` / `after` ではなく **`before_value` / `after_value`**。
Oracle のキーワードと衝突する読み方を避けた。意味は仕様のまま。

## 2. Object Storage と PAR

### 決めたこと 4: 再生は PAR。API は映像を中継しない

`GET /api/video/assets/{id}/playback` が期限付き URL を返す（既定 1 時間・天井 24 時間）。
API がバイト列を中継すると Container Instance のメモリと帯域を食い、シーク再生（Range）も
自前で実装することになる。実機で Range が効くことを確認した（`HTTP 206` / 1024 bytes）。

**PAR のトークンは作成時の応答にしか入っていない**（後から引き直せない）ので、再生要求ごとに
発行する。溜まらないように寿命を短く保ち、映像の削除時に `_purge_objects` がプレフィックス
配下の PAR をまとめて消す。削除後に発行済み URL を叩くと `HTTP 401` になることを確認した。

### 決めたこと 5: Content-Type を付けて置く

付けないと Object Storage は `application/octet-stream` を返し、URL を開いても**再生ではなく
ダウンロード**になる（＝完了条件を満たさない）。`mimetypes` で判定し、`video/*` のときだけ採る。

実ブラウザ（Chromium）で確認（`scenario-6.txt`）:

```
readyState = 4 (HAVE_ENOUGH_DATA) / duration = 2 / currentTime = 1.203 / paused = false
videoWidth = 320 / videoHeight = 240 / error = null
```

### 決めたこと 6: 削除は「本体が先・台帳が後」

逆順にすると Object Storage 側の削除が落ちたときに**誰からも辿れない本体**が残る（課金され
続け、次の削除でも消せない）。この順なら失敗時は台帳行が残るだけで、もう一度 DELETE すれば
片付く。単体テストで「オブジェクト削除が落ちたら行を残す」ことを固定した。

サムネイルは後続タスクが `video/<owner>/<id>/thumb/...` に増やすので、本体 1 個ではなく
**プレフィックス配下を全部**消す（`next_start_with` で全ページ辿る）。

### 決めたこと 7: 時刻は UTC に寄せて保存し、"Z" を付けて返す

`captured_at` / `created_at` は `TIMESTAMP`（タイムゾーン無し）。オフセット付きの入力
（`+09:00` / `Z`）をそのまま渡すと、同じ瞬間でも入力のオフセット次第で別の壁時計時刻として
保存され、後続タスクの期間検索（`specs/20` §4 の `captured_from/to`）が静かにずれる。
入口（`video.to_utc_naive`）で UTC へ寄せ、`created_at` の既定値も `SYS_EXTRACT_UTC(SYSTIMESTAMP)`
にした。返すときは `TO_CHAR(..., 'YYYY-MM-DD"T"HH24:MI:SS"Z"')` で時間帯を明示する
（付けないと受け手がローカル時刻と解釈して 9 時間ずれる）。**`AT TIME ZONE` は使わない** ——
素の `TIMESTAMP` に掛けるとセッションの時間帯で解釈されてから変換され、UTC で入れた値が動く。

実機で JST 入力が UTC で返ることを確認した: `captured_at=2026-08-19T19:00:00+09:00`
→ 応答・一覧・詳細のいずれも `2026-08-19T10:00:00Z`（`scenario-7.txt`）。

### 決めたこと 8: PAR は全ページ集めてから消す

再生要求のたびに PAR が増えるので、削除時に 1 ページ目だけ消すと**よく再生された映像ほど
PAR が消し残る**（＝台帳から消えた後もその URL で読める）。さらに、辿りながら消すとページ位置が
件数に依存するため詰めた分が飛ばされる。**全ページ集めてから削除する。**
オブジェクト側の `next_start_with` は名前カーソルなのでこの問題は起きない（そちらは逐次で可）。
単体テストの fake は 1 ページ 1 件でページングし、この 2 つの誤りを両方落とせるようにした。

### 決めたこと 9: 本文をメモリに読み切らない

`UploadFile.file` をストリームのまま `put_object` へ渡す。バイト列に読み切ると映像 1 本分が
そのままコンテナ（4GB）のメモリに載る。上限は 500MB（ADR-0032「まず短い映像で成立させる」）。

## 3. 所有者分離

既存の `owner_sub` の流儀（`rag` / `minutes` / `demos`）どおり、SQL の `WHERE owner_sub = :o` で
強制する。他人の映像は 403 ではなく**存在しない扱い**（所有者以外に id の存在有無を漏らさない）。
実機で `get_asset` / `playback_url` が `None`、`delete_asset` が `False` を返し、他人の
オブジェクトが手つかずで残ることを確認した。

## 4. E2E の実施環境（正直な範囲）

| 層 | 使ったもの | 備考 |
|---|---|---|
| ADB | `jetuse-loop-adb`（internal-dev / ap-osaka-1・DSN `jetuseloop2_low`） | ループ固定環境。migration を実適用 |
| Object Storage | **`jetuse-pubdev-video`（public-dev / us-chicago-1）** | apply で作られた正規バケット |
| 映像 | `imageio-ffmpeg` で生成した 2 秒 / 320x240 / 11,401 バイトの mp4 | 実ファイル |
| API | `uvicorn service.main:app` を起動し **実 HTTP** で叩く | 配備済みコンテナではない（下記） |

Object Storage 層は **apply 済みの正規バケット `jetuse-pubdev-video`** を使った。DB 層は
ループ固定環境（`jetuse-loop-adb`）のまま —— public-dev の ADB（`jetusepubdev`）は ORM スタックが
生成した資格情報で保護されており、ループはそれを持たない（推測で触らない）。migration の適用は
配備時に API コンテナの起動処理が行う。

実施できなかった範囲と理由は `runs/2026-08-19T2225_VID-01/e2e/SKIPPED.md` に明記した。
残る 1 点は **public-dev に配備済みの API コンテナ**への E2E（配備自体が後続の人間ゲート）。
これは同じ `service.main:app` を uvicorn で起動し実 HTTP で叩くことで、ルーティング・認証依存
（`AUTH_REQUIRED=true` で 5 経路すべて 401）・`VIDEO_BUCKET` 配線（未設定で 503）・
`/api/health` の `schema=ok` まで確認した（`scenario-7.txt`）。残る差分は Container Instance の
`resource_principal` 権限と API Gateway 経由の疎通で、これは配備後でないと確かめられない。

後片付け: この run が作った `video_assets` / `video_scenes` 行はすべて削除（残 0）。
`jetuse-pubdev-video` に残ったオブジェクト・PAR は 0。apply 前に代替として使っていた
`jetuse-spike-vid01` バケットも削除済み（`oci os bucket get` が `BucketNotFound`）。

## 5. リージョンの落とし穴（実機で踏んだ）

`sdk_signer_args(region)` は **`config_file` モードでは region 引数を使わない**（`~/.oci/config` の
プロファイル値が効く）。そのためローカルから大阪プロファイルでシカゴのバケットを触ると
`BucketNotFound` になる。`genai.py` / `tts.py` と同じく `args["config"]["region"]` を明示して直した。

`rag.py` / `minutes.py` は同じ明示をしていない。配備時（`resource_principal`）は region が効くので
実害は出ていないが、**ローカルから配備先リージョンのバケットを触ると同じ穴に落ちる**。
VID-01 の範囲外なので触っていない（後続または別タスクへ）。

## 6. 残っている人間ゲート

**public-dev への配備そのもの**（API コンテナ）。バケットの apply は済んでいるので、
配備すれば `VIDEO_BUCKET` が注入され（`infra/orm/locals.tf`）、この機能はそのまま動く。
配備前は `VIDEO_BUCKET` が空なので API は 503（「映像機能は未設定です」）を返す。
未設定と故障を混ぜないための挙動で、`require_video` が `require_speech` と同じ形で実装している。

`/api/health` の capabilities には **video を足していない**。バケットが存在しない現時点で足すと、
既に配備済みのスタックがすべて `ok=false` になる（`speech` が未設定時にそうなるのと同じ形）。
apply 後に足すのが筋なので、後続タスクへ送る。

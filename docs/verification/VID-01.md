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

---

# VID-02 追記: 場面分割とフレーム抽出（ffmpeg）

実施日: 2026-08-20 / リージョン: us-chicago-1（Object Storage）/ ap-osaka-1（ADB）/
対象: `jetuse_core/video_frames.py` + `jetuse_core/video.py`（分析の入口）+ migration 024・025・026
仕様の正本: `specs/20-video-search.md` §3（1〜2 と「同時実行の範囲」）/ 判断: ADR-0032 決定2・決定3
証跡: `runs/2026-08-19T2336_VID-02/e2e/`

## 結論（先に）

実映像から場面（時間区間）を実測で切り出し、各場面の代表フレームとサムネイルを作って
実 Object Storage へ置けるところまでを実 OCI で確認した。**時刻はここで確定する** ——
境界も尺も `ffmpeg` の出力から作り、LLM は 1 度も呼んでいない（ADR-0032 決定3）。

**壊れた映像・音声のみのファイルは理由付きで失敗する。** 「場面 0 件で正常終了」には
ならず、`analysis_state=failed` と `analysis_error` を残す。

**1 つの映像に対する分析は同時に 1 つだけ**（specs/20 §3「同時実行の範囲」）。
`analysis_state` を条件に含めたアトミックな UPDATE が入口で、取れなかった側は
`AnalysisInProgressError`（API は 409）になる。

| 完了条件 | 結果 | 証跡 |
|---|---|---|
| 実映像で場面数と境界が妥当 | ○ 15 秒 / 3 カットが 5.0s・10.0s で 3 分割 | `e2e/scenario-1.txt` |
| 場面サムネイルが Object Storage に入る | ○ 実バケットへ 3 枚（11,206 / 3,778 / 15,367 バイト） | `e2e/scenario-1.txt` / `e2e/scenario-1-scene-0000.jpg` |
| `VIDEO_ASSETS.duration_ms` を埋める | ○ 14,900ms（解像度 320x240・fps 10.07 も実測） | `e2e/scenario-1.txt` |
| 転換が無い映像でも 1 場面 | ○ 6 秒 1 カット → `[0, 6000)` の 1 件 | `e2e/scenario-2.txt` |
| 壊れた映像で握りつぶさない | ○ `VideoDecodeError` ＋ `failed` ＋ 理由 | `e2e/scenario-3.txt` |
| 音声のみで握りつぶさない | ○ 同上（「映像ストリームがありません」） | `e2e/scenario-3.txt` |
| 長すぎる区間の分割 | ○ 転換なし 70 秒 → 23,333ms × 3 | `e2e/scenario-4.txt` |
| 再分析（同じ入口） | ○ 世代が入れ替わり、台帳と実体が一致 | `e2e/scenario-5.txt` |
| **同じ映像への分析は同時に 1 つだけ** | ○ 2 本目は `AnalysisInProgressError` | `e2e/scenario-6.txt` |
| 取り残された `running` を固めない | ○ 古い `running` は引き継ぎ、新しいものは弾く | `e2e/scenario-7.txt` |
| **引き継がれた側は何も書かない** | ○ `AnalysisSupersededError`・場面も状態も無変化 | `e2e/scenario-9.txt` |
| 保存後に引き継がれても新しい世代を消さない | ○ 掃除は「自分が置く前から在ったもの」だけ | `e2e/scenario-9.txt` |
| 分析中の削除（v1 の範囲外） | ○ 決めたとおり。残骸は回収路が引き取る | `e2e/scenario-8.txt` |
| migration 023 → 024 → 025 → 026 の適用 | ○ **使い捨てスキーマ**へ素の状態から順に適用 | `e2e/scenario-migration.txt` |
| 併合・分割の境界条件の単体テスト | ○ `test_video_frames.py` 72 件 | — |
| `ffmpeg` が無い / 失敗したときの扱い | ○ 依存欠落・バイナリ未解決・起動不能・タイムアウトを別々に | `test_video_frames.py` |
| `make lint` / `make test` | ○ api 1,221 件パス・ruff クリーン | — |

## 1. 決めたこと: 時刻を作るのは 1 回の復号パス

尺・解像度・fps と場面転換を、**1 回の `ffmpeg` 起動**で同時に測る。

```
ffmpeg -hide_banner -nostdin -i <path> -an \
       -filter:v "select='gt(scene,0.4)',showinfo" -f null -
```

`-f null -` で出力を捨てながら全フレームを復号し、標準エラーに出る入力ヘッダ
（`Duration:` / `Stream #0:0 ... Video: ... 320x240 ... 10.07 fps`）と `showinfo` の
`pts_time:`（選ばれたフレームの時刻）を両方読む。分けて 2 回呼ぶと同じ映像を 2 度復号する。

**入力側の記述だけを読む。** `ffmpeg` は出力側にも `Stream ...: Video: ...` を出すので、
全文から拾うとフィルタ後の解像度・fps を映像の素性として記録してしまう。
`\nOutput #` で切ってから解析している（単体テストに番人を置いた）。

## 2. 決めたこと: 定数の根拠

| 定数 | 値 | 根拠 |
|---|---|---|
| `SCENE_THRESHOLD` | 0.4 | 実測で本物のカット変わりが 0.69〜0.71、同一カット内の揺れが 0.05〜0.08。0.3 以下はカメラの動き・照明変化を拾って細切れになり、0.5 以上は似た画どうしのカットを逃す |
| `MIN_SCENE_MS` | 2,000 | 2 秒未満の帯はタイムラインで掴めず、再生しても内容を確認できない（specs/20 §6 の用途）。場面ごとに視覚 LLM 呼び出しとサムネイルが要るので、確認できない粒度まで割るのは払い損 |
| `MAX_SCENE_MS` | 30,000 | 転換検出は「画が変わったか」しか見ない。定点カメラ・長回しでは画が同じまま内容が変わる。代表フレーム 3 枚（10 秒間隔）で区間を賄える上限として 30 秒 |
| `FRAMES_PER_SCENE` | 3 | 1 枚だと転換直後のフェード中のフレームを掴んで区間を代表しないことがある。視覚 LLM のトークンは枚数に比例するので 3 枚に留める |

`MAX_SCENE_MS` の効きは実測した。転換の無い 70 秒の映像が 23,333ms × 3 に割れる
（`scenario-4.txt`）。**AI Vision は使えないと実測済み**（ADR-0032 決定1・2026-08-20 改訂）
なのでラベル区間での境界補正は無く、この分割が「同じ画のまま内容が変わる」への唯一の
手当てになる。

## 3. 決めたこと: 分析の入口は 1 本（specs/20 §3「同時実行の範囲」）

`video.claim_analysis` の**アトミックな 1 文の UPDATE** が入口。

```sql
UPDATE video_assets
   SET analysis_state = 'running', analysis_started_at = SYS_EXTRACT_UTC(SYSTIMESTAMP),
       analysis_error = NULL
 WHERE id = :id AND owner_sub = :o
   AND (analysis_state <> 'running' OR analysis_started_at IS NULL
        OR analysis_started_at < :stale)
```

読んでから書く 2 段にすると、その隙間に相手も同じ判定を通れてしまう。取れなければ
`AnalysisInProgressError`（API は 409）、映像が無い・他人のものなら `LookupError`
（所有者以外に id の存在有無を漏らさない）。実機では、1 本目が走っている最中に 2 本目を
投げると `analysis_state=running` を見て弾かれ、場面行も重複しなかった（`scenario-6.txt`）。

**`analysis_started_at`（migration 025）は取り残された `running` を引き継ぐために要る。**
条件が `analysis_state <> 'running'` だけだと、分析中にプロセスが落ちた映像は `running` の
まま固まり、**二度と再分析できなくなる**（要求8 が死ぬ）。開始時刻を持てば「十分に古い
`running` は引き継いでよい」と言える。実機で、`ANALYSIS_STALE_SECONDS`（2 時間）より古い
`running` は引き継げ、いま始まったものは弾かれることを確認した（`scenario-7.txt`）。

**引き継ぎは「相手が落ちている」ことを保証しない**（単に遅いだけかもしれない）。生きたまま
引き継がれた古い実行がそのまま台帳を書き続けると、新しい実行の場面を上書きし、その
`running` まで解いて 3 本目の開始を許す —— **「同時に 1 つだけ」が結果として破れる**。
そこで権利を取るたびに `analysis_token`（migration 026）へ新しい印を書き、以降の書き込みは
**その印が一致するときだけ**通す。

- `_save_scenes` は `... AND analysis_token = :tok FOR UPDATE` で照合してから書く
  （行ロックは、照合から書き込みまでに引き継ぎが割り込まないようにするため）
- `finish_analysis` も同じ印を条件に持つので、引き継がれた側は新しい実行の `running` を
  解けないし、自分の失敗で `failed` に落とすこともできない
- 印が合わなければ `AnalysisSupersededError` で、**1 行も書かずに降りる**

実機で、サムネイルを置く前に別の実行が印を取り直した状況を作ると、古い実行は
`AnalysisSupersededError` で終わり、`VIDEO_SCENES` は無変化・`analysis_state` は新しい実行の
`running` のままだった（`scenario-9.txt`）。

**時刻を印に流用しない。** python-oracledb は `datetime` を既定で `DATE` として束縛するので、
`TIMESTAMP(6)` 列との等値比較が小数秒の欠落で**静かに 0 件になる**（実測。`setinputsizes` で
回避はできるが、書き忘れた瞬間にフェンスが黙って効かなくなる）。文字列なら型の取り違えが
起きないので、`analysis_token` は `VARCHAR2(64)` にした。

**終わったら `pending` に戻す。** ここで割れたのは場面（specs/20 §3 の 1〜2）だけで、
説明・要約・埋め込み（3〜6）はまだ走っていない。`done` にすると、説明の無い場面を
「分析済み」として見せることになる。分析全体を束ねる後続タスクは、同じ `claim_analysis` を
外側で 1 回取って `done` / `partial` を書く。

## 4. 決めたこと: サムネイルの入れ替えは世代で行う

分析 1 回ぶんを世代（`thumb/<generation>/`）に閉じ込め、**置く → 台帳を切り替える →
自分が置く前から在った分を消す**の順で入れ替える。先に消すと、その後の復号・アップロード・
DB 更新のどこかで落ちた瞬間に、台帳の `thumb_object` が消えたオブジェクトを指す
（再分析するまで直らない）。途中で落ちたときは**何も消さない** —— 台帳は前回の世代を
指したままで画面は壊れず、置き去りは次に成功した分析が引き取る。

**掃除の対象は「自分が置く前から在ったもの」に限る。** 「台帳が指していないもの」を対象に
すると、権利を引き継いだ別の実行が置いたばかりの世代（まだ台帳に載っていない）まで消して
しまい、その実行が台帳を書いた瞬間に `thumb_object` が消えたオブジェクトを指す。
台帳への書き込みは権利の印で守られているが、**掃除は行ロックの外で走る**ので、対象そのものを
安全な集合に狭めるのが確実だった。自分より前から在ったものなら、後から始まった実行の成果を
巻き込みようがない。実機で、台帳を書いた直後に引き継ぎが起き、引き継いだ側が新しい世代を
置いた状況を作っても、その世代が残ることを確認した（`scenario-9.txt` の (b)）。

## 5. 範囲を決めて削ったもの（specs/20 §3「同時実行の範囲」）

**分析の実行中に同じ映像を削除すること、およびそこで残った残骸の即時回収は v1 の範囲外**
と決めた。理由は仕様に書いたとおり —— 完全に閉じるには Object Storage（トランザクションが
無い）と DB をまたぐ分散トランザクションか、映像ごとの外部ロックが要る。実害は
**残骸オブジェクトが数個残ること**だけで、データは壊れない。

**握りつぶすのとは違う。** 起きないことにするのではなく、`reap_orphan_assets`
（台帳に行の無い映像 id の配下を、`ORPHAN_GRACE_S` 経過後に引き取る）が後から回収すると
決めた。回収できなかったものは `logger.error` で名指しして残す。分析の前段でこの回収路を
毎回回している。

実機で確かめた。サムネイルを置き終えた後に削除が走る順序では、削除側の掃除がすべて拾って
残骸は出なかった。回収路そのものを通すために残骸を明示的に置くと、`reap_orphan_assets` が
それを引き取り、残り 0 になった（`scenario-8.txt`）。

**この判断に伴って削ったコード**（凝った防御を残すほうが、読めない・壊れやすいコードになる）:

| 削ったもの | 何を守っていたか |
|---|---|
| 掃除の線引きを経過時間で行う `ORPHAN_GRACE_S` ベースの sweep | 並行する再分析どうしが互いの世代を消し合うこと（置いている最中の世代を避ける） |
| 分析全体の締切 `ANALYSIS_DEADLINE_S` | 上の猶予より実行時間が長くならないことの担保 |
| 失敗時に「自分が置いた分」を台帳と突き合わせて引き取る経路 | `commit` の応答喪失時に成功した台帳を宙参照に変えないこと |
| `delete_asset` の「空を確かめてから行を消す」2 回掃除＋失敗時の非成功返し | 削除と分析の TOCTOU |

`ORPHAN_GRACE_S` は残したが、意味を **「登録の途中（本体を置いてから台帳へ行を入れるまで）を
巻き込まないための余白」** だけに絞り、1 時間へ下げた。

**一度削った `pre_existing` 方式は、形を変えて戻した**（上記 §4）。削った時点では
「並行する再分析どうしが互いの世代を消し合う」ことへの不十分な防御だったが、権利の印で
台帳への書き込みが 1 本に絞られた後は、**掃除の対象を安全な集合に狭めるいちばん単純な規則**
になった。台帳を読み直す必要も、経過時間で線を引く必要も無い。

## 6. 実機で踏んだ落とし穴

### 代表フレームの時刻を末尾の余白でクランプすると、区間の外へ出ることがある

低 fps の映像では末尾の余白（実測 fps の 2 フレームぶん）が場面より広くなりうる
（0.5fps なら 4 秒。`MIN_SCENE_MS` は 2 秒）。素直にクランプすると `start_ms` より前を指し、
**隣の場面の絵をこの場面のサムネイルとして保存する**。区間を優先し、そこに本当にフレームが
無ければ `extract_frame` が理由付きで落ちるようにした —— 誤った絵を黙って保存するより、
失敗として見えるほうがよい。

### 尺を越えた位置を指すと `ffmpeg` は「成功したまま 0 バイト」を返す

14.90 秒の映像に `-ss 14.85` を渡すと **終了コード 0 / 標準出力 0 バイト**になる
（最後の復号可能なフレームが 14.8 秒）。そのまま保存すると壊れた JPEG がサムネイルとして
バケットに残り、画面には壊れた画像が出るだけで原因が分からない。対策は 2 つ掛けてある。
(a) 代表フレームの時刻を実測 fps から求めた 2 フレーム分だけ末尾から空ける、(b) それでも
0 バイトや JPEG でないものが返ったら例外にする。

### `mjpeg` は full-range を要求する

`-vf scale=...` を付けずに `-c:v mjpeg` へ流すと
`Non full-range YUV is non-standard, set strict_std_compliance ...` で符号化器が開けない
映像がある。`-pix_fmt yuvj420p` を明示して固定した。

### 単色の映像では場面スコアが立たない

赤 → 緑のような単色ベタの切り替わりは `scene_score` が 0.0 になり、閾値を 0.05 まで
下げても検出されなかった。**検証用の映像は中身のある画で作る**（`testsrc2` /
`smptebars` / `mandelbrot`）。単色で作ると「検出できている」つもりのまま閾値を誤って詰める。

## 7. migration 024 / 025

### 024: 場面の区間を「実測から作った正しい区間」だけに絞る

023 の `CHECK (end_ms >= start_ms)` は **負の開始時刻とゼロ長の場面を通す**。どちらも
タイムライン表示と「その時刻から再生」に不正な値を渡す。場面を実際に作るのは VID-02 なので、
`start_ms >= 0 AND end_ms > start_ms` を `video_scenes_span_ck` として足した。

**`ALTER TABLE ... ADD CONSTRAINT` の 1 文だけにする。** 023 の制約は落とさず、厳しい方を
隣に足す。理由は 2 つある。

1. Oracle の DDL は 1 文ごとに暗黙 commit されるので、`DROP` → `ADD` の 2 文にすると、
   `ADD` が失敗した瞬間（既存行が新しい制約に反する等）に**制約の無い表が残る**。
   この隙間はトランザクションでは塞げない。
2. 2 文の間で接続が切れると「片方だけ適用され、`schema_migrations` には記録が無い」状態に
   なり、再実行が別の理由で落ちる。1 文なら**適用されたか、されていないか**の 2 状態しかない。

残る `video_scenes_range_ck`（`end_ms >= start_ms`）は新しい制約に含意されるので、両方が
有効でも矛盾しない。

### 025 / 026: 分析の入口に要る `analysis_started_at` と `analysis_token`

上記「§3 分析の入口は 1 本」のとおり。どちらも `ALTER TABLE ... ADD` の 1 文だけ。
025 は「十分に古い `running` は引き継いでよい」と言うための開始時刻、026 は引き継ぎが
起きたときに**古い実行を黙らせる**ための権利の印。

### 検証は使い捨てスキーマで行う

**既存スキーマの制約を DROP して巻き戻す検証はしない**（通常フロー外の DROP は人間ゲート）。
代わりに `JETUSE_SPIKE_VID02_<乱数>` を作り、**素の状態（表 0 個）から `001`〜`026` を順に
適用**して、出来上がった制約・列・境界値を確かめ、そのスキーマごと捨てた
（`scenario-migration.txt`）。

- `VIDEO_SCENES_SPAN_CK` = `start_ms >= 0 AND end_ms > start_ms` が乗り、023 の
  `VIDEO_SCENES_RANGE_CK` も残っている
- `VIDEO_ASSETS.ANALYSIS_STARTED_AT` = `TIMESTAMP(6)` / NULL 可、
  `VIDEO_ASSETS.ANALYSIS_TOKEN` = `VARCHAR2` / NULL 可
- 負値・ゼロ長・逆順は `ORA-02290` で落ち、`start_ms=1000 / end_ms=1001` は通る

作成・削除の根拠は CLAUDE.md「検証用リソースの作成・削除（`jetuse-spike-` プレフィックス
必須）」と `loop-config.yml` の例外（接頭辞つき・run 固有・証跡に記録）。**名前は実行ごとに
一意**にしてある —— 固定名だと、並行する run や前回の残骸を「自分のもの」と誤認して壊しうる。
既に同名が在れば作らずに止まる。この run が作ったスキーマだけを消し、既存スキーマには
触れていない。

## 8. 依存の入れ方（ADR-0032 決定2 の実行）

`imageio-ffmpeg` を `packages/api/pyproject.toml` の `dependencies` に足した。
**`apt-get install ffmpeg` にしない** —— `Containerfile` の「変わりにくい層（依存）→
変わりやすい層（アプリ）」というレイヤ分割を崩し、アプリだけ直したときのビルド時間
（42 分 → 82 秒にした成果）を目減りさせるため。

`ffmpeg` が使えないことは、映像が壊れていることと**別の例外**にしてある
（`FfmpegUnavailableError` / `VideoDecodeError`）。前者は配備の不備で、映像を差し替えても
直らない —— 同じ例外にすると利用者が「この映像が悪い」と誤解する。

## 9. 実施できなかった範囲

`runs/2026-08-19T2336_VID-02/e2e/SKIPPED.md`。要点は、(1) VID-02 は API エンドポイントを
足していないので HTTP 経由の E2E は対象外（`/analyze` と 409 への対応付けは後続）、
(2) 長時間・大量映像の所要時間は ADR-0032 の「未解決」のまま、(3) public-dev の ADB への
024 / 025 / 026 適用は配備時（資格情報をループは持たない）、(4) 分析中の削除の即時整合は
specs/20 §3 で範囲外と決めたもの。

後片付け: この run が作った `video_assets` / `video_scenes` 行とオブジェクトはすべて削除
（`cleanup.txt`：残 0）。検証用スキーマ `JETUSE_SPIKE_VID02` も削除済み。
なお `deploy_cmd` の `ops/start-adb-if-stopped.sh` を引数なしで実行したため、
`jetuse-dev-adb`（internal-dev / us-chicago-1）も起動している。E2E で使ったのは
`jetuse-loop-adb`（ap-osaka-1）だけで、共有 ADB を止めるかは人間の判断に返す。

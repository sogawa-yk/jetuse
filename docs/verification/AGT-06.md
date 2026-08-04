# AGT-06 検証レポート — シカゴ移設とエージェント対応モデル

- 実施日: 2026-08-03
- 実施者: ループ(maker=Claude / checker=Codex)
- リージョン: `us-chicago-1`(比較のため `ap-osaka-1` も同条件で測定)
- コンパートメント: `jetuse-proto`(`.env` の `COMPARTMENT_OCID`)
- 認証: `config_file`(ローカル `~/.oci/config`)
- 生データ: `runs/2026-08-03T1125_AGT-06/e2e/probe-*.json` / `agt06-e2e.json` /
  `agt06-compare.json` / `terraform-plan-*.log`

> **この文書に載っている可否は、すべて上記の日付に実機で測った結果である。**
> 推測・ドキュメント引用による「動くはず」は書いていない。

---

## 1. なぜ測り直したか — 登録簿が実機と食い違っていた

`packages/api/jetuse_core/models.py` は「Responses API 対応は `gpt-oss-120b` だけ、
gemini 系は chat(=Responses 非対応)」と記録していた。この記録を根拠に
**エージェントで選べるモデルは 1 つだけ**になっていた。実機で当たると誤りだった。

| モデル | 旧登録簿の記録 | 実機(2026-08-03) |
|---|---|---|
| `openai.gpt-oss-120b` | responses | responses **OK**(記録どおり) |
| `openai.gpt-oss-20b` | **未登録** | responses **OK** |
| `google.gemini-2.5-pro` | chat(=非対応扱い) | responses **OK**(要・差の吸収) |
| `google.gemini-2.5-flash` | chat | responses **OK**(要・差の吸収) |
| `meta.llama-3.3-70b-instruct` | chat | Responses は **404**(記録どおり) |

誤記録の実害は「モデルの限界かプロンプトの問題か切り分けられない」こと。
案件デモで観測した壊れ方(同じ検索を 12 回繰り返し、架空の品番を作って手順書を返す)が
モデル固有なのか判断できなかった。**§4 の比較で、これはモデル差だと言えるようになった。**

---

## 2. シカゴと大阪の品揃え

`ListModels`(CHAT / ACTIVE)の件数。

| リージョン | CHAT/ACTIVE | 大阪に無い系統 |
|---|---|---|
| `us-chicago-1` | **47** | **xai(Grok)系 16 種**・`meta.llama-4-*` |
| `ap-osaka-1` | 29 | (シカゴに無い: `gemini-3.x` 系・`cohere.command-a-vision` 等) |

ADR-0001 は「大阪では Grok 系・Llama 4 系が使えない」と記録している。**これは大阪の話で、
シカゴには当てはまらない。** ただし一覧に載ることと使えることは別で、§3 で個別に当たっている。

**一覧に載っていても非推奨日を過ぎているものがある。** シカゴの Grok のうち
`grok-3*` / `grok-4` / `grok-4-fast-*` / `grok-4-1-fast-*` / `grok-code-fast-1` は
`timeDeprecated=2026-05-15`(既に経過)。**登録簿にはこれらを入れていない。**
入れたのは非推奨日が設定されていない `grok-4.3` / `grok-4.20-{reasoning,non-reasoning}` のみ。

---

## 3. エージェント適性(Responses API・シカゴ)

測った軸は 4 つ。

- **responses**: Responses API に到達できるか(404 でないか)
- **system**: `type=message, role=system` の入力アイテムを受け付けるか
- **tools**: 入れ子引数つき function tool を呼べるか(`{"part":..,"opts":{"qty":..}}`)
- **roundtrip**: 返ってきた `function_call` と `function_call_output` を積み直した
  続きの往復が通るか(**stream=True で**)

| モデル | responses | system | tools(入れ子) | roundtrip | 登録簿 |
|---|---|---|---|---|---|
| `openai.gpt-oss-120b` | OK | OK | OK | OK | エージェント可 |
| `openai.gpt-oss-20b` | OK | OK | OK | OK | エージェント可 |
| `xai.grok-4.3` | OK | OK | OK | OK | エージェント可 |
| `xai.grok-4.20-reasoning` | OK | OK | OK | OK | エージェント可 |
| `xai.grok-4.20-non-reasoning` | OK | OK | OK | OK | エージェント可 |
| `google.gemini-2.5-pro` | OK | **400** | OK | **400**(id 付き) | 可・**要吸収** |
| `google.gemini-2.5-flash` | OK | **400** | OK | **400**(id 付き) | 可・**要吸収** |
| `google.gemini-2.5-flash-lite` | OK | **400** | OK | **400**(id 付き) | 可・**要吸収** |
| `xai.grok-4.20-multi-agent` | OK | OK | **400** | — | **不可**(理由つきで断る) |
| `meta.llama-4-maverick-*` | **404** | — | — | — | 載せない(当コンパートメント未提供) |
| `meta.llama-4-scout-*` | **404** | — | — | — | 載せない |
| `meta.llama-3.3-70b-instruct` | **404** | — | — | — | chat 専用・エージェント不可 |
| `cohere.command-a-03-2025` | **404** | — | — | — | 載せない |
| `cohere.command-latest` | **404** | — | — | — | 載せない |

エラー本文:

- gemini の system: `400 Unable to submit request because Content with system role is not supported.`
- gemini の roundtrip: `400 Invalid JSON data: ... data did not match any variant of untagged enum ResponseInput`
- `grok-4.20-multi-agent`: `400 {"code":"invalid-argument","error":"Client-side tools for multi-agent models require beta access"}`

### 3.1 `ResponseInput` の 400 は「stream かつ `id` 付き」でだけ起きる

タスク起票時に大阪で観測されていた `untagged enum ResponseInput` の 400 は、
**モデル差そのものではなく、積み直すアイテムの形の差**だった。切り分けた結果:

| モデル | stream | `function_call` に `id` を含める | 結果 |
|---|---|---|---|
| gemini-2.5-pro / flash | false | 含める | **OK** |
| gemini-2.5-pro / flash | **true** | **含める** | **400 ResponseInput** |
| gemini-2.5-pro / flash | true | 含めない | **OK** |
| gpt-oss-120b / grok-4.3 / grok-4.20-reasoning | true / false | どちらでも | OK |

`chat.py` の `_collect_hop_events` は `type/name/arguments/call_id/id` を通していたので、
**エージェントは常に `id` 付きで積み直していた**。gemini はストリーミングで動かすため、
そのままでは必ず 400 になる。`call_id` は `function_call_output` との対応付けに要るので残し、
`id` だけを落とす(`jetuse_core/model_compat.py`)。

> **想定と違った点**: 当初は「gemini は積み直しの形式そのものが違う」と考えていたが、
> 非ストリーミングでは `id` 付きでも通る。ストリーミング限定の差だったため、
> 非ストリーミングだけで確認していたら見逃していた。

### 3.2 system ロールの畳み込みは、実際の入力形でも効く

役割だけを `user` へ移す(本文は変えない)。**先頭(エージェントの人格)と末尾
(打ち切り時の force-answer)の両方**で確認した。

| 位置 | 畳み込みあり | 畳み込みなし(現行コードの挙動) |
|---|---|---|
| 先頭に指示 | **OK**(入れ子引数つきツールを正しく呼ぶ) | 400 |
| 末尾に force-answer | **OK**(最終回答を返す) | **400** |

末尾の系は `_force_answer_message()` が出すもので、ホップ上限に当たったときだけ通る経路。
ここを畳まないと「普段は動くが、上限に当たったターンだけ 400」になる。

---

## 4. 多段の手続きでのモデル比較(定量)

案件デモの壊れ方を誘発する架空の受注手続きを用意し、**同一シナリオを 8 モデル × 3 試行**で流した
(`spikes/agt06/compare.py`)。`order_context_initialize` が正しい品番 `PX-9001` を返すが、
**検索には出てこない**ので、戻り値を使わないと完了できない。デコイの品番は検索に出る。

| モデル | 完了 | 順序平均(3点満点) | 平均ツール呼出 | 検索 | 架空の品番 | 拒否 | 平均秒 |
|---|---|---|---|---|---|---|---|
| `gpt-oss-120b` | **3/3** | 3.0 | 3.0 | 0 | 0 | 0 | 4.0 |
| `gpt-oss-20b` | **1/3** | 1.0 | 1.0 | 0 | 0 | 0 | 3.5 |
| `grok-4.3` | **3/3** | 3.0 | 3.0 | 0 | 0 | 0 | 5.0 |
| `grok-4.20-reasoning` | **3/3** | 3.0 | 3.0 | 0 | 0 | 0 | 7.6 |
| `grok-4.20-non-reasoning` | **3/3** | 3.0 | 4.3 | 1 | 0 | **3** | 4.8 |
| `gemini-2.5-pro` | **3/3** | 3.0 | 3.0 | 0 | 0 | 0 | 11.8 |
| `gemini-2.5-flash` | **3/3** | 3.0 | 3.0 | 0 | 0 | 0 | 5.4 |
| `gemini-2.5-flash-lite` | **3/3** | 3.0 | 3.0 | 0 | 0 | 0 | 4.1 |

読み取れること:

- **`gpt-oss-20b` は多段の手続きに耐えない。** 3 試行中 2 回は**ツールを 1 度も呼ばず**、
  「どの品番を設定しますか？キーワードを教えてください」と**利用者に聞き返して終わった**。
  デモで観測した「実行せず手順書を返す」と同じ壊れ方で、これは**モデル差**である。
  一覧に出しても既定にはしない。
- 残り 7 モデルは 3/3 完了。**この難度では差がつかない**(最短経路 3 呼び出しをそのまま踏む)。
- `grok-4.20-non-reasoning` だけノイズが多い(平均 4.3 呼出・拒否 3 回・検索 1 回)。
  ただし**拒否されたあと自力で正しい品番へ戻れている**(自己修正あり・全試行完了)。
- `gemini-2.5-pro` は正しいが**遅い**(11.8 秒。最速の約 3 倍)。
- **架空の品番を作ったモデルは 1 つも無かった**(24 試行中 0)。デモで起きた捏造は、
  この難度では再現しない。より長い手続き・より多い候補で再測が要る(§7)。

> n=3 は当たり外れを均すには小さい。**傾向として読むこと。**

---

## 5. 登録簿に入れた各フラグの裏取り

「動くはず」を書かないため、`ModelDef` の各フラグを個別に測った。

| モデル | reasoning effort | 画像1枚 | 画像2枚 | 非推奨日 |
|---|---|---|---|---|
| `gpt-oss-120b` | **OK** | 400(非対応) | 400 | - |
| `gpt-oss-20b` | **OK** | 400(非対応) | 400 | - |
| `grok-4.3` | **OK** | OK | OK | - |
| `grok-4.20-reasoning` | **400**(`does not support parameter reasoningEffort`) | OK | OK | - |
| `grok-4.20-non-reasoning` | **400**(同上) | OK | OK | - |
| `gemini-2.5-pro/flash/flash-lite` | **400**(`thinking_level is not supported`) | OK | OK | - |
| `meta.llama-3.2-90b-vision-instruct`(chat 経路) | — | OK | **400**(`At most 1 image`) | **2026-05-15(経過済)** |

> **注意**: 名前に `reasoning` と付く `grok-4.20-reasoning` が `reasoning effort` を
> **受け付けない**。名前から推測して `reasoning=True` にすると 400 になる。
> 登録簿では `grok-4.3` / `gpt-oss-*` だけを `reasoning=True` にした。

> **`meta.llama-3.2-90b-vision-instruct` は登録簿で唯一、非推奨日を過ぎている。**
> 現時点では応答するが、シカゴでは後継(画像対応)の選定が要る。

画像は 32×32 の単色 PNG で測った。**1×1 では Grok が
`Image dimensions 1x1 are too small` で弾く**ため、小さすぎる画像だと「非対応」と誤判定する。

---

## 6. リージョンをまたぐ機能の可否

| 機能 | `us-chicago-1` | `ap-osaka-1` | 備考 |
|---|---|---|---|
| GenAI 推論(Responses / Chat) | **OK** | OK | §3 |
| GenAI プロジェクト(CP・Vector Store 本体) | **OK** | OK | project は**リージョン別**(下記) |
| Document Understanding(OCR) | **OK** | OK | `analyze_document` 成功 |
| Speech STT(文字起こし・到達性) | **OK** | OK | ジョブ一覧が引ける |
| **TTS(音声合成)** | **NG(HTTP 500)** | **NG(HTTP 404)** | 下記 |

### 6.1 TTS はシカゴでも動かなかった(記録と食い違う)

リポジトリは 2026-07-28 の実測として「`us-chicago-1` で合成成功」と記録していた
(`packages/api/jetuse_core/tts.py` 冒頭)。**2026-08-03 に測り直すと 500 InternalError。**

- アプリ本体の経路(`tts.probe()` / `tts.synthesize()`)で、`list_voices`・`synthesize` とも 500
- 2 回試行しても同じ。手組みリクエストでも SDK 経由でも同じ
- 大阪は 404(記録どおり)、**`us-phoenix-1` は成功(6783 バイトの MP3)**

**アプリの挙動としては壊れない。** `TTS_REGION` 未指定時の自動フォールバック(FIX-58)が
「デプロイリージョン → `us-phoenix-1`」の順に試すため、シカゴ配備でも Phoenix で合成できる:

```
candidates: ['us-chicago-1', 'us-phoenix-1']
probe:      {"ok": true, "region": "us-phoenix-1"}
synthesize: OK bytes=6783 resolved=us-phoenix-1
```

**したがって TTS のためにコードを変える必要は無い。** ただし
「シカゴで TTS が動く」という記録は**現時点では誤り**なので、`TTS_REGION` を
シカゴに固定してはいけない(固定すると TTS が死ぬ)。

### 6.2 project はリージョンごとに別物

`OpenAi-Project` ヘッダに入れる GenerativeAiProject OCID は**リージョン別**。
大阪の project をシカゴへ送ると `400 Invalid OpenAI project`。
非 OpenAI モデル(gemini / grok)は `OpenAi-Project` **必須**で、無いと
`400 Non-OpenAI models require 'OpenAI-Project'`。
シカゴには既に `jetuse-loop-project` が ACTIVE で存在するため、**新規作成はしていない。**

---

## 7. Terraform(シカゴ配備の plan)

**apply は実行していない**(人間ゲート)。plan のみ。

| スタック | 結果 | 内訳 |
|---|---|---|
| `infra/orm`(ワンクリック) | **plan 成功** | **169 to add, 0 to change, 0 to destroy** |
| `infra/terraform/environments/dev`(共有基盤) | **plan 成功** | **33 to add, 0 to change, 0 to destroy** |
| `infra/terraform/environments/app`(開発者ごと) | **plan 不能** | 下記 |
| `infra/terraform/environments/loop` | validate 成功 | plan は共有基盤に依存 |

- **0 to destroy** — シカゴの plan は大阪の資産を一切壊さない(別 state・別リージョン)。
- plan 出力に `ap-osaka-1` は **0 件**。イメージ参照は `ord.ocir.io/...`(シカゴの OCIR)へ
  自動解決された。**リージョン依存の直書きは残っていない。**
- `environments/app` は `../dev/terraform.tfstate`(共有基盤の state)を読むため、
  **シカゴの共有基盤を apply するまで plan できない**(`Unable to find remote state`)。
  これは構造上の順序であって設定の誤りではない。手順は
  `docs/guides/osaka-teardown.md` §2。

### 7.1 直書きの洗い出し(`ap-osaka-1` の grep)

| 箇所 | 直書きの種類 | 対応 |
|---|---|---|
| `.env.example` `OCI_REGION` / `GENAI_BASE_URL` | 既定値 | シカゴへ変更 |
| `jetuse_core/settings.py` `oci_region` | 既定値 | シカゴへ変更 |
| `environments/{app,dev}/variables.tf` `region` | 既定値 | シカゴへ変更 |
| `app/alice.tfvars.example` | 例 | シカゴ + `ord.ocir.io` へ |
| **`ops/deploy-agent-containers.sh` `REGION=ap-osaka-1` + `kix.ocir.io`** | **上書き不能** | `ops/_region.sh` で解決(下記) |
| **`ops/redeploy-agent-env.sh` 同上** | **上書き不能** | 同上 |
| **`ops/deploy-hosted-agent.sh` `kix.ocir.io` + `OCI_REGION` 直値** | **上書き不能** | 同上 |
| `ops/_adb.py` / `packages/agent-containers/*` / `hosted-agent-sample` | 既定値 | シカゴへ変更 |
| `spikes/*` | 既定値 | **変更せず**(過去タスクの一回限りの検証記録) |

**最も実害があったのは ops の 3 スクリプト。** リージョンとイメージの置き場が別々に
直書きされていたため、`REGION` だけ変えても**イメージは大阪の OCIR を指したまま**になる。
`ops/_region.sh` に一元化し、リージョン → OCIR ホストの対応(`ord`/`kix`/`nrt`/`iad`)を
1 か所で解決する。未対応リージョンは `OCIR_HOST` の明示指定を促して**停止する**(fail-closed)。

---

## 8. 実環境 E2E

`spikes/agt06/e2e.py`。実装(`stream_agent` / `http_tools`)をそのまま呼ぶ。
相手は公開 https エコー、モデルはシカゴの実物。証跡 `runs/2026-08-03T1125_AGT-06/e2e/`。

| シナリオ | 結果 |
|---|---|
| 1. Grok が入れ子引数つき**外部 HTTP ツール**を呼ぶ | **OK**(`grok-4.3`。相手に入れ子のまま到達) |
| 2. 同一シナリオを複数モデルで比較 | **OK**(6 モデル全て成功。§4 の定量比較は別途 3 試行) |
| 3. エージェント不可のモデルは断られる | **OK**(`grok-4.20-multi-agent` / `llama-3.3-70b` とも理由つき) |
| 4. 回帰: `gpt-oss-120b` | **OK**(1 呼び出しで完了。挙動不変) |

実施できなかった範囲は `runs/2026-08-03T1125_AGT-06/e2e/SKIPPED.md`。

---

## 9. 登録簿を増やすとき(手順)

**測ってから書く。** 新しいモデルを足す前に、そのモデルに対して次を回すこと。

```bash
# 0. 前提: .env に AGT06_CHICAGO_PROJECT_OCID(対象リージョンの GenAI project)

# 1. エージェント適性(responses / system / 入れ子 tools / roundtrip の id 有無)
PROBE_OUT=probe.json PYTHONPATH=spikes/agt06 \
  .venv/bin/python spikes/agt06/probe_caps.py <oci-model-id>

# 2. 各フラグ(reasoning effort / 画像1枚 / 画像2枚)
PROBE_OUT=flags.json PYTHONPATH=spikes/agt06 \
  .venv/bin/python spikes/agt06/probe_flags.py <oci-model-id>

# 3. 多段の手続きに耐えるか(既定 3 試行。MODELS_UNDER_TEST に足してから)
COMPARE_OUT=cmp.json PYTHONPATH=packages/api \
  .venv/bin/python spikes/agt06/compare.py

# 参考: リージョンをまたぐ機能の可否
PROBE_OUT=svc.json PYTHONPATH=spikes/agt06 \
  .venv/bin/python spikes/agt06/probe_services.py us-chicago-1 ap-osaka-1
```

確認できなかったフラグは **false のままにする**。`ListModels` に載っていることは
使えることの証拠ではない(§2・§3 で 404 になったものが実際にある)。
`timeDeprecated` が過去日のモデルは登録しない。

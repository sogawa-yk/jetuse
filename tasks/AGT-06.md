# タスク: AGT-06 JetUse をシカゴへ移し、エージェントで複数モデルを使えるようにする

## 目的
**エージェントに使えるモデルが `gpt-oss-120b` の 1 つしかない。** モデル登録簿が
「他は Responses API 非対応」としているが、**実機で確かめたら誤り**だった。
あわせて、**大阪より品揃えの厚いシカゴへ JetUse 本体を移す**（ADR-0016 §E で合意済みの方針）。

案件デモの実測で、**デモの天井がモデル側にある**兆候が出ている:
`order_context_initialize` の応答に `"requested_product_code":"IPHONE18"` が返っているのに、
モデルはそれを使わず**同じ検索を 12 回**繰り返し、最後に**架空のコード `APL47726` を作り出して**
実行せずに手順書を返した。**比較できる別モデルが無いと、これがモデルの限界なのか
プロンプトの問題なのか切り分けられない。**

## 実機で確認した事実（推測ではない）

### 1. 登録簿の記録が古い（大阪・`OpenAi-Project` ヘッダ付きで実測）
| モデル | 登録簿 | 実機 |
|---|---|---|
| `openai.gpt-oss-120b` | responses | **OK** |
| `openai.gpt-oss-20b` | 未登録 | **OK**（関数呼び出し可） |
| `google.gemini-2.5-pro` | chat（＝非対応扱い） | **OK**（関数呼び出し可） |
| `google.gemini-2.5-flash` | chat | **OK** |
| `meta.llama-3.3-70b-instruct` | chat | NG（記録どおり 404） |

### 2. シカゴの品揃えは大阪より厚い（CHAT/ACTIVE を列挙）
`xai.grok-4.3` / `grok-4` / `grok-4.20-*` / `grok-code-fast-1` / `meta.llama-4-*` /
`gemini-2.5-*` / `cohere.command-a-*` / `gpt-oss-120b|20b` など **40 件超**。
大阪に無い **Grok 系・Llama 4 系**がある（ADR-0001 で「大阪不可」と記録した系統）。

### 3. シカゴで**入れ子引数つきの関数呼び出し**が動くことを実測
検証用プロジェクト（`jetuse-spike-chicago-probe`。**確認後に削除済み**）を作って実測:

| モデル | 結果 |
|---|---|
| `xai.grok-4.3` | **OK**（`{"part":"JX-7742","opts":{"qty":3}}` を生成） |
| `xai.grok-4` / `grok-4-fast-reasoning` / `grok-4.20-reasoning` | **OK** |
| `google.gemini-2.5-pro` | **OK** |
| `openai.gpt-oss-120b` | **OK** |
| `xai.grok-4.20-multi-agent` | **NG**（`Client-side tools` 非対応） |
| `meta.llama-4-*` / `cohere.command-a-03-2025` | NG（404。当コンパートメントでは未提供） |

**TOOL-03 で対応した入れ子引数が、Grok でもそのまま通る**ことまで確認した。

### 4. 「非 OpenAI モデルは project ヘッダが要る」
`OpenAi-Project` を付けないと `400 Non-OpenAI models require 'OpenAI-Project' …`。
**project はリージョンごと**で、大阪の project をシカゴへ送ると `400 Invalid OpenAI project`。
＝ **シカゴで使うにはシカゴの project が要る**（移設が要る理由のひとつ）。

### 5. 1 行の差し替えでは済まない（大阪の gemini で実測）
モデル登録簿を responses に変えてデモを流したところ:
- `400 Content with system role is not supported`（**system ロールを受け付けない**）
- 最初の 1 呼び出しは成功したが、**続きの往復で `400 … did not match any variant of untagged
  enum ResponseInput`**（ツール結果を積み直す形式が受け付けられない）

**モデルごとの差を吸収する層が要る。**

## 仕様参照
- `packages/api/jetuse_core/models.py`（`MODELS` 登録簿・`ApiFamily`）
- `packages/api/jetuse_core/genai.py`（`make_inference_client` / `resolve_project_ocid`）
- `packages/api/jetuse_core/chat.py`（`stream_agent` のホップ・ツール結果の積み方）
- `infra/terraform/environments/`（配備先リージョン）
- `docs/decisions/ADR-0001-*.md`（大阪の制約。**Grok 不可の記述はシカゴでは当てはまらない**）
- `docs/decisions/ADR-0016`〜（JetUse をシカゴで提供する方針＝合意済み）

## 対象 area
api（＋ infra）

## 前提（依存タスク / 人間の事前作業）
- AGT-05 マージ済み。
- **`terraform apply` と大阪リソースの削除は人間ゲート。** ループは plan までで止まり、
  実行は人間が行う。**ループが destroy / delete を実行してはならない。**

## 決まっていること（2026-08-03 人間ゲートで承認済み。作り直さない）

1. **JetUse 本体をシカゴ（`us-chicago-1`）へ移す。大阪の配備は撤去する。**
2. **シカゴで使えるモデルをエージェントで使えるようにする**（Grok 系を含む）。
3. モデル登録簿を**実機の結果に合わせて直す**（`gemini` / `gpt-oss-20b` の誤記録を含む）。

## 作業内容

### A. リージョン移設
- 配備先を `us-chicago-1` にできるようにする（Terraform 変数・`.env.example`・ドキュメント）。
- **シカゴに project / ADB / Vector Store / Container Instance / API Gateway / Vault を用意する。**
  リージョン依存の値がコードに直書きされていないか洗い出す（`ap-osaka-1` の grep）。
- **大阪の撤去は最後**。シカゴが動いてから、人間が実行する（**ループは plan と手順書まで**）。
- **リージョンをまたぐ機能の可否を実測して表にする**（TTS は大阪不可・シカゴ可否は未確認、
  Document Understanding、Select AI 等）。**「動くはず」で書かない。**

### B. モデル登録簿と互換層
- 登録簿を**実機の結果に合わせる**。少なくとも `gemini-2.5-pro` / `gemini-2.5-flash` /
  `gpt-oss-20b` は Responses 対応、`grok` 系を追加。**確認していないものは追加しない。**
- **モデルごとの差を吸収する**:
  - `system` ロール非対応のモデルでは、指示を利用者メッセージ側へ畳む（内容は変えない）。
  - ツール結果を積み直す形式のモデル差（`ResponseInput` の受け付け方）を吸収する。
  - **吸収できない差は「そのモデルではエージェント不可」と登録簿に持たせ、要求時に断る。**
    黙って動かないより、断るほうがよい。
- `xai.grok-4.20-multi-agent` は **client-side tools 非対応**（実測）。**エージェント不可として扱う。**

### C. 比較ドキュメント（プリセールス転用可能な粒度）
- `docs/comparison/agent-capable-models.md` を作る。**モデル × 可否 × 実測の根拠**。
  「どのモデルなら複雑な業務 API を捌けるか」に答えられる形にする。
- 可能なら**同一シナリオで定量比較**（正しい順序で呼べた本数・自己修正回数・検索回数・往復数）。

## 完了条件（検証可能な述語で）
- [ ] シカゴ配備の Terraform が `plan` を通り、**リージョン依存の直書きが残っていない**。
- [ ] シカゴで JetUse が起動し、`/api/health` が正常（実環境）。
- [ ] **エージェントで Grok 系のモデルが動く**（入れ子引数つきの外部 HTTP ツールを呼べる）。
- [ ] `gemini` 系がエージェントで動く（`system` ロール非対応を吸収できている）。
- [ ] **エージェント不可のモデルは要求時に断る**（黙って壊れない）。
- [ ] 登録簿の内容が**すべて実機で確認済み**（未確認のものが載っていない）。
- [ ] 大阪の撤去手順書があり、**撤去前に消してよいものの一覧と、消してはいけないものが分かれている**。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス・`.venv/bin/ruff check packages/api` クリーン。
- [ ] STATE.md の `review_verdict` が PASS。

## E2E シナリオ（実環境）
- [ ] シナリオ1: シカゴで、**入れ子引数を持つ外部 HTTP ツールを Grok に呼ばせる**。
- [ ] シナリオ2: 同じシナリオを **2 つ以上のモデルで流し、結果を比較表にする**。
- [ ] シナリオ3: エージェント不可のモデルを指定すると**断られる**ことを示す。
- [ ] シナリオ4（回帰）: `gpt-oss-120b` の挙動が変わっていないことを示す。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記（無言スキップ禁止）。

## 成果物
- Terraform / 設定 / `models.py` / 互換層の変更、テスト
- `docs/decisions/ADR-00xx`（**リージョン移設の判断**と、モデル差の吸収方針）
- `docs/comparison/agent-capable-models.md`（上記 C）
- `docs/verification/AGT-06.md`（**実機の測定値**。登録簿が誤っていた経緯を含む）
- **大阪撤去の手順書**（人間が実行する。何を消し、何を残すか）

## 判断を上げること（実装せず ADR に書いて止める）
- **エージェントの既定モデルをどれにするか**（コストと精度の兼ね合い。比較の結果を添えて）。
- **大阪を完全に撤去するか、一部を残すか**（大阪限定デモの需要が残るなら残す判断もある）。

## 非ゴール / 禁止事項
- **`terraform apply` / `destroy` を実行しない。大阪のリソースを削除しない**（人間ゲート）。
- **既存リソース**（VCN `develop`、インスタンス `dev`、バケット `jetuse-oci-source-documents`）を
  変更・削除しない。
- **確認していないモデルを登録簿に足さない**（「動くはず」を書かない）。
- 案件デモのモック・ゲートウェイ（大阪の `mnpdemo-*`）を壊さない。移設対象は JetUse 本体。
- **顧客名・案件名を書かない**（公開リポジトリ）。認証情報・OCID をコミットしない。
- IAM を変更しない（必要なら blocked として報告）。未承認のコミット / PR / push を行わない。

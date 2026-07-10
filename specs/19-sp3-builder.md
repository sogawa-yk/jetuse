# specs/19: SP3 — ビルダー詳細仕様（ヒアリング → デモ設計 → フロント生成 → データ生成 → Demo 産出）

> 状態: ドラフト（人間レビュー待ち — SP3-00 の人間ゲート。承認までは SP3-01 以降を起動しない）。
> 日付: 2026-07-07。
> 上位: specs/17-demo-platform-redesign.md §1・§3〜§6・§7 / ADR-0015・ADR-0016。
> 前提実装: SP1（能力カタログ `GET /api/capabilities`・`DemoContext` seam・デモスコープ chat/rag 縦切り）、
> SP2（Demo エンティティ CRUD・箱の lazy プロビジョニングと後始末・排他リース・VPD・
> デモスコープ dbchat・Identity Domains 実接続 — specs/18）。
> 比較ドキュメント: docs/comparison/frontend-generation-runtime.md（§4.4 の論点整理。**決定は SP3-03 の ADR**）。

## 0. 位置づけ・スコープ

SP3 は Internal 固有（`dev` 枝 — specs/17 §7。ステージ統合ブランチ `feat/sp3-builder`）。
specs/17 §6 の概略「ヒアリング → 能力カタログを LLM に渡してデモ設計 → OpenCode + OCI モデルで
静的SPA生成 → サンプルデータ生成・投入 → `Demo` として保存」を実装可能な粒度に確定する。

| タスク | 本仕様の対応節 |
|---|---|
| SP3-01 ビルダー・パイプライン API 骨格 + ヒアリング(NL) | §1・§2・§8 |
| SP3-02 デモ設計（能力カタログ→デモプラン生成） | §3 |
| SP3-03 フロント生成（OpenCode + OCI モデル）+ 配信 | §4・§5（**方式は ADR — §4.4**） |
| SP3-04 サンプルデータ生成 + 箱への投入 | §6 |
| SP3-05 ビルダー UI + デモ産出 E2E | §7 |

**スコープ境界**: マーケットプレイス（SP4）・`connector.invoke`・統一 Capability インターフェースは対象外。
Public（main）の user 単位ルートのパス・挙動は変えない。生成するのは**フロント（静的SPA）とデータのみ**
（バックエンド生成なし — ADR-0015 決定2・3）。

### 0.1 判断が割れる論点と本仕様での扱い（一覧）

| 論点 | 扱い | 節 |
|---|---|---|
| ヒアリングセッションの持ち方（専用エンティティ vs 会話流用） | **本仕様で決定**（専用エンティティ） | §2.1 |
| プラン語彙（8能力全部 vs デモスコープ実装済み3系統） | **本仕様で決定**（3系統で開始） | §3.4 |
| 生成の同期/非同期 | **本仕様で決定**（非同期・202 + status ポーリング）。実行体は ADR | §4.4 |
| OpenCode のランタイム（コンテナ内 subprocess / ジョブごと専用コンテナ / 常駐ワーカー） | **SP3-03 の ADR に委譲**（要件・受け入れ条件のみ本仕様が定義） | §4.4 |
| 生成サンドボックスの方式・LLM 認証経路 | **SP3-03 の ADR に委譲** | §4.3・§4.4 |
| バンドル配信（API 経由 vs PAR 直配信） | **本仕様で決定**（API 経由） | §5.2 |

## 1. パイプライン全体像（SP3-01 が骨格を敷く）

### 1.1 フェーズ・成果物・状態

```
①ヒアリング(NL)          ②デモ設計               ③生成(非同期)                    ④確認・確定
 builder_session          builder_session          Demo(status=provisioning)        Demo(status=ready)
 status=hearing     ──▶   status=designed    ──▶    a. データ投入(§6)         ──▶   プレビュー(/app/)
 成果物=要求サマリ         成果物=デモプラン          b. フロント生成(§4)              PATCH name/desc
 (requirements JSON)      (plan JSON)               c. 静的検査→バンドル公開(§5)      or DELETE(破棄)
                                                    失敗 ─▶ status=failed（§1.3）
```

- 中間アーティファクトはすべて機械可読 JSON: **要求サマリ**（§2.2）→ **デモプラン**（§3.2）→
  **Demo.config.plan + config.frontend**（§5.3）。各フェーズの入力は前フェーズの成果物のみ
  （フェーズ境界で疎結合。後段は前段の LLM 対話履歴に依存しない）。
- `Demo` 行が生まれるのは**③生成開始時点**（`status='provisioning'` — specs/18 §1.2 が SP3 予約
  した状態の活性化）。①②の間はビルダーセッション（§2.1）だけが状態を持つ。
  理由: データ・バンドルの投入先（箱 `demo_<id>` / バンドル prefix）が demo_id を要求するため、
  生成より前に Demo を作る必要があり、逆にヒアリング中に空の Demo を作ると破棄セッションが
  demos 一覧を汚す。
- **公開 `POST /api/demos` の契約（INSERT のみ・即 ready — specs/18 §3.1）は変えない**。
  ビルダーは内部のリポジトリ関数（`create_demo(..., status='provisioning')` — 内部専用引数）で作る。

### 1.2 Demo.status の SP3 遷移（specs/18 §1.2 予約状態の活性化）

| 遷移 | 契機 | 実装 |
|---|---|---|
| （なし）→ `provisioning` | ビルダーの生成開始（§4.5）が Demo 行を INSERT | 内部専用の作成経路 |
| `provisioning` → `ready` | ③a〜c がすべて完走（静的検査合格・バンドル公開済み） | 既存 `set_status`（楽観遷移） |
| `provisioning` → `failed` | ③のいずれかの失敗・タイムアウト・検査不合格 | 同上。失敗理由は `config.generation.error`（秘密を含めない要約文字列）に記録 |
| `failed` → `provisioning` | 再生成（§1.3。同一セッションから `generate` を再実行） | 同上 |
| `ready` / `failed` → `deleting` | DELETE（specs/18 §3.2 — 既存のまま） | 既存 |

- `provisioning` 中の DELETE: 既存の排他リース（specs/18 §3.2.1）で直列化される。生成ジョブは
  **フェーズ境界（③a/b/c の各前後）で `status='provisioning'` を再確認**し、`deleting` を観測したら
  即座に中止する（後始末は DELETE 側が完走 — 生成側は補償削除を持たない。生成途中の残骸は
  すべて specs/18 §3.2 + §5.4 の後始末が exact 根拠で回収できる実体に限る）。
- **能力ルートと `/app/` 配信は `status='ready'` のみ通す**（§8.1 — deleting 404 の一般化）。

### 1.3 失敗の扱い・巻き戻し

- **巻き戻し専用の機構は作らない**。失敗時は `failed` に落とすだけで、途中まで作られた箱の中身
  （datasets 行・RAG ファイル・staging バンドル）はそのまま残す。理由: 後始末は既に
  「DELETE で収束」（specs/18 §3.2）と「再生成で置換」（§6.3・§5.1）の 2 経路で完結しており、
  第 3 の巻き戻し機構は追加部品に見合わない。
- **再生成 = 冪等な上書き**: `failed → provisioning` で③を頭から再実行する。データ投入は
  同名 dataset / 同名文書を「削除 → 再作成」で置換（§6.3）、バンドルは新 bundle_id で生成して
  ポインタ切替（§5.1）— どの時点で失敗しても再実行で収束する。
- **破棄 = DELETE**: specs/18 §3.2 の後始末（+ §5.4 のバンドル prefix）が全て消す。

## 2. ヒアリング(NL)の契約（SP3-01）

### 2.1 ビルダーセッション = 専用エンティティ（決定）

| 案 | 評価 |
|---|---|
| **A: 専用エンティティ `builder_sessions`（採用）** | 構造化状態（要求サマリ・プラン・demo_id 紐付け）が主役で、対話履歴は従。1 行に閉じる。既存 conversations の契約（user 経路の `demo_id IS NULL` 強制・demo 会話の箱紐付け — specs/18 §4.2）に触らない |
| B: conversations 流用 + メタ列追加 | 対話履歴の保存は既存流用できるが、要求サマリ/プランの置き場に結局別表かメタ列が要り、user/demo 会話の分離契約（specs/18 §4.2 で確定済み）へ第 3 の会話種別を差し込む改修になる。分離契約の再検証コストが利点を上回る |

**スキーマ**（単文 migration 規律 — specs/18 §1.1 と同じ理由。番号は SP3-01 実装時点の最新に採番。
ランナーの事後条件検証・fault-injection の対象に含める）:

```sql
-- 025_builder_sessions.sql（1 文）
CREATE TABLE builder_sessions (
  id          VARCHAR2(36) PRIMARY KEY,
  owner_sub   VARCHAR2(255) NOT NULL,
  status      VARCHAR2(20) DEFAULT 'hearing' NOT NULL
              CONSTRAINT ck_bs_status CHECK (status IN ('hearing','designed')),
  transcript  CLOB DEFAULT '[]' NOT NULL CONSTRAINT ck_bs_transcript CHECK (transcript IS JSON),
  requirements CLOB CONSTRAINT ck_bs_requirements CHECK (requirements IS JSON),
  plan        CLOB CONSTRAINT ck_bs_plan CHECK (plan IS JSON),
  demo_id     VARCHAR2(36),
  created_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
)
-- 026_builder_sessions_idx.sql（1 文）
CREATE INDEX idx_bs_owner ON builder_sessions(owner_sub, updated_at)
```

- `owner_sub` は**識別列**（demos.owner_sub と同じ raw sub — specs/18 §4.2 のキー列区別。
  資源キー列ではないので `owner_key` ヘルパーは通さない）。所有者強制は usecases/demos の流儀
  （SQL の WHERE 句・0 行 = 404 存在秘匿）。
- セッションの状態機械は **`hearing` → `designed` の 2 状態のみ**。生成以降の進行は
  `demo_id` の有無と `Demo.status`（§1.2）から導出する（状態の二重管理をしない）。
- `demo_id` 設定後（生成開始後）のセッションは**読み取り専用**（messages / design / generate は 409）。
  再生成だけは §4.5 の再実行契約で許す。
- **transcript の上限**（信頼境界の入力上限）: 発話 1 件 ≤ 4,000 文字（超過 422）、
  セッション合計 50 往復 or transcript 直列化 256KB 到達で 422（「新しいセッションを開始して
  ください」を detail に含める）。LLM 入力の有界化を兼ねる。

### 2.2 収集項目と要求サマリ

ヒアリングが埋める **要求サマリ（requirements JSON）**:

```json
{
  "industry":        "製造",
  "use_case":        "設備保全のナレッジ検索と故障履歴の照会",
  "capabilities_hint": ["rag.search", "dbchat"],
  "data_profile": {
    "documents": "保全マニュアル・作業手順書の類",
    "tables":    "設備台帳・故障履歴（設備ID/日時/症状/対応）"
  },
  "notes": "現場の保全員が使う想定。専門用語は残してよい"
}
```

| フィールド | 意味 | 十分性判定での必須 |
|---|---|---|
| `industry` | 業種 | **必須** |
| `use_case` | デモで見せたい業務・ユースケース | **必須** |
| `capabilities_hint` | 使いたい能力の候補（カタログの capability id。LLM が対話から推定。**拘束しない** — 確定は §3 のデモ設計） | 任意 |
| `data_profile.documents` / `data_profile.tables` | データの雰囲気（RAG 文書 / DB 表それぞれ） | **どちらか一方以上必須** |
| `notes` | その他の要望 | 任意 |

### 2.3 「設計に足りる」判定（fail-closed）

- 毎応答で LLM が構造化出力（temperature 0）を返す:
  `{"reply": "<追質問 or 確認の応答文>", "requirements": {…}, "sufficient": bool, "missing": ["…"]}`。
- **サーバ側の決定的再検査を最終判定とする**: LLM が `sufficient=true` を返しても、§2.2 の必須
  フィールドが空なら `sufficient=false` に落とす（LLM の判定だけを信頼境界にしない）。
  逆（必須が埋まっているのに LLM が false）は LLM に従う（追加確認したい合理的な場合がある）。
- `sufficient=true` になってもヒアリングは継続できる（追加発話で requirements を更新）。
  §3 の設計は `sufficient=true` のセッションでのみ実行可能（そうでなければ 409）。

### 2.4 API（すべて `require_user`。Internal 専用面 — AUTH_REQUIRED=true 前提）

| Method/Path | 成功レスポンス | 備考 |
|---|---|---|
| `POST /api/builder/sessions` | SessionOut（201 相当の JSON） | Body なし。`status='hearing'`・transcript=[] で INSERT |
| `POST /api/builder/sessions/{sid}/messages` | `{"reply": str, "requirements": {…}, "sufficient": bool, "missing": [str]}` | Body `{"content": str}`。LLM 1 呼び出し（同期 JSON。SSE 化は SP3-05 の UX 要件が求めたら residual） |
| `GET /api/builder/sessions/{sid}` | SessionOut | 現在状態（下記） |

- **SessionOut**: `{"id", "status", "transcript": […], "requirements": {…}|null,
  "plan": {…}|null, "demo_id": str|null, "demo_status": str|null, "created_at", "updated_at"}`。
  `demo_status` は demo_id があるとき JOIN で添える（UI の進行表示用 — §7）。
- 越境は **404 `{"detail": "session not found"}` の存在秘匿**（demos と同形）。401 は require_user。
- セッション一覧 API は SP3-05 の UI 要件が確定してから（v1 の UI は直近セッションを
  localStorage で覚える想定 — §7）。必要になった時点で `GET /api/builder/sessions`（自分の所有のみ）
  を足す（residual）。
- LLM は既存 chat 基盤（genai.py）の既定モデルを流用。モデル選択 UI は持たない（YAGNI）。

## 3. デモ設計の契約（SP3-02）

### 3.1 入力と実行

- `POST /api/builder/sessions/{sid}/design`（`require_user` + 所有者、`sufficient=true` でなければ 409、
  `demo_id` 設定済みなら 409）。
- 入力 = 要求サマリ + **能力カタログの機械可読部分**（`GET /api/capabilities` と同じ生成関数を
  内部呼び出し。§3.4 の語彙でフィルタ）+ 固定のプラン産出プロンプト（版数管理 — §4.2 の再現性）。
- LLM（temperature 0）にデモプラン JSON を生成させ、§3.3 の検証を通ったものを
  `builder_sessions.plan` に保存して `status='designed'`。検証不合格は**同一リクエスト内で
  最大 2 回まで再生成**（検証エラーをフィードバックして再試行）し、なお不合格なら 422
  （detail に検証エラー要約。transcript は消さない — ヒアリング続行で改善できる）。
- 再実行可能: `designed` 後の追加発話（§2.3）→ `design` 再実行でプランを上書きできる
  （`demo_id` が付くまで）。

### 3.2 デモプラン（機械可読スキーマ・plan_version=1）

```json
{
  "plan_version": 1,
  "title": "設備保全アシスタント",
  "description": "保全マニュアル検索と故障履歴照会を 1 画面で見せるデモ",
  "capabilities": ["chat", "rag.search", "dbchat"],
  "screens": [
    {
      "id": "home",
      "title": "保全デスク",
      "description": "検索と照会を並べたメイン画面",
      "blocks": [
        {"type": "rag.search", "title": "マニュアル検索",
         "suggested_prompts": ["ポンプ P-102 の分解手順は?"]},
        {"type": "dbchat", "title": "故障履歴照会",
         "suggested_prompts": ["直近3ヶ月で故障が多い設備は?"]},
        {"type": "chat", "title": "保全アシスタント",
         "system_prompt": "あなたは設備保全の専門アシスタント。…",
         "suggested_prompts": ["予知保全の始め方を教えて"]}
      ]
    }
  ],
  "data": {
    "tables": [
      {"name": "equipment", "title": "設備台帳", "rows": 50,
       "columns": [
         {"name": "equipment_id", "type": "VARCHAR2(20)", "description": "設備ID"},
         {"name": "installed_on", "type": "DATE", "description": "設置日"}
       ]}
    ],
    "documents": [
      {"filename": "maintenance_manual.md", "title": "保全マニュアル",
       "outline": "章立て: 安全注意 / 日常点検 / 分解整備 / トラブルシュート"}
    ]
  }
}
```

**配線はブロック型で固定する（自由配線をさせない）**: `blocks[].type` = capability id であり、
「そのブロックがどのエンドポイントをどう叩くか」は**フロント側スキャフォールドの固定 API
クライアント（§4.3）が型ごとに決める**。プランに URL・パス・HTTP の自由記述フィールドは
存在しない（範囲外呼び出しの構造的防止の一部 — §4.3）。

### 3.3 検証（pydantic strict / extra=forbid / fail-closed）

| 対象 | 制約 |
|---|---|
| `plan_version` | `== 1` 固定（未知版は 422） |
| `capabilities` | **§3.4 の語彙の部分集合**（未知・語彙外は 422。空も 422） |
| `screens` | 1〜5 画面。`blocks` は画面あたり 1〜8。`blocks[].type ∈ plan.capabilities` |
| `data.tables` | 0〜5 表。列 1〜20。`rows` 1〜500。表・列名は `^[a-z][a-z0-9_]{0,29}$`（保守的な識別子のみ — SQL/命名機構への信頼境界）。表名の重複禁止 |
| `columns[].type` | 許可リストのみ: `VARCHAR2(n CHAR)`（n≤1000）/ `NUMBER` / `NUMBER(p[,s])` / `DATE` / `TIMESTAMP`（datasets 機構が投入可能な型に閉じる） |
| `data.documents` | 0〜10 件。`filename` は `^[a-z0-9_-]{1,64}\.(md|txt)$`。`title` ≤ 200 文字 |
| データ整合 | `dbchat ∈ capabilities` ⇔ `tables ≥ 1`、`rag.search ∈ capabilities` ⇔ `documents ≥ 1`（能力があるのにデータ定義がない/逆は 422） |
| 文字列長 | title ≤ 200 / description・outline ≤ 1000 / system_prompt ≤ 4000 / suggested_prompts 各 ≤ 200・5 件まで |
| 全体 | プラン直列化 ≤ 256KB（Demo.config ≤1MB — specs/18 §2.2 — に余裕を残す） |

上限値の根拠: 箱あたり上限（specs/18 §3.1 — datasets 10 / files 20）より十分内側に置き、
生成時間・LLM 入力を有界化する。上限は設定値とし、既定を上表とする。

### 3.4 プラン語彙 = 「デモスコープルートを持つ demo_safe 能力」（決定）

語彙はカタログから**構造的に導出**する: `demo_safe=true` かつ `routes` に
`/api/demos/{demo_id}/` 配下のパスを 1 つ以上持つ能力。現時点では **chat / rag.search / dbchat の
3 系統**（specs/18 §4.3 が SP2 の完了形とした主要 3 系統と一致）。

| 案 | 評価 |
|---|---|
| **A: 3 系統で開始（採用）** | 生成フロントの呼び出し先が全て `/api/demos/{id}/` に閉じる（§4.3 のスコープ担保が成立）。追加コストゼロ。チャット+文書検索+DB照会で顧客業務デモの主要形は組める |
| B: stateless 系（translate / docunderstand / voice / minutes）へデモスコープのパススルーを先に追加して語彙を 8 能力に広げる | 追加はルート + ディスクリプタ追記のレシピ（specs/18 §4.3）どおり安いが、SP3 の検証面積（生成テンプレート・静的検査・E2E）が能力数ぶん増える。ビルダーの縦切り成立が先 |
| C: 語彙は 8 能力に広げ、stateless 系は user 単位ルートを叩かせる | **不採用**。「生成物は当該デモの `/api/demos/{id}/` スコープしか叩けない」（§4.3）という構造担保が最初から破れる |

**推奨 = A**。語彙導出が構造的（カタログの routes 実在から導く）ため、後で B のパススルーを
足せば語彙は自動で広がり、プランスキーマ・検証・スキャフォールドはブロック型の追加だけで追従する
（語彙拡張は SP3 の非ゴール — §11。residual として記録）。

### 3.5 プランの決定性・テスト

- temperature 0 + スキーマ検証 + 有界再試行（§3.1）で「同じ要求サマリ → 検証合格プラン」を安定させる。
  完全一致の再現は要求しない（LLM 出力の揺れは検証が吸収する）。
- 単体テスト（fake LLM）: 語彙外能力を含む出力 → 422 / 再試行で合格 / 識別子・型・上限の境界 /
  「dbchat あり tables なし」等の整合違反 / スナップショット（固定 fake 出力 → 検証済みプラン）。

## 4. フロント生成（SP3-03）— 要件・非機能・安全要件

**実装方式（OpenCode のランタイム・サンドボックス実体・LLM 認証経路）は本仕様では確定しない。
SP3-03 冒頭で ADR（OpenCode 統合方式)を起草し人間承認を得てから実装する（§4.4）。**
本節は方式によらず満たすべき要件＝ ADR の受け入れ枠を定義する。

### 4.1 機能要件

| # | 要件 |
|---|---|
| F1 | 入力 = 検証済みデモプラン（§3.2）+ demo_id + **固定スキャフォールド**（§4.3 — API クライアント・最小デザイン・ビルド設定を同梱したテンプレート）。**JetUse の秘密・OCID・エンドポイント実値・.env は入力に含めない** |
| F2 | 生成器 = **OpenCode（headless / CLI 実行）+ OCI 大阪リージョンの OpenAI 互換 agentic API のモデル**（CLAUDE.md 記載のベース URL 配下）。OpenCode はスキャフォールド上でプランの画面・ブロックを実装する。**使用モデルは設定（settings / env）で切り替え可能とし、コードに固定しない**（既定モデルの選定は SP3-03 の ADR。spec 承認条件 2026-07-07） |
| F3 | 出力 = **ビルド済み静的SPAバンドル**（index.html + アセット。デモ専用サーバコードなし）。`/api/demos/{demo_id}/app/` 配下で配信されて動く（相対パス前提のビルド設定） |
| F4 | 生成フロントは同一オリジンの**相対パスで当該デモの `/api/demos/{demo_id}/` 配下のみ**を呼ぶ（§4.3 S3）。chat/rag は SSE ストリーミング対応 |
| F5 | UI 言語は日本語。プランの title/description/suggested_prompts を反映する |
| F6 | 生成は**非同期**（§4.5）。失敗・タイムアウト・検査不合格は `failed`（§1.2） |

### 4.2 非機能条件

| # | 条件 | 値（既定。SP3-03 で実測して確定値を検証レポートに記録） |
|---|---|---|
| N1 | 生成時間 | 目標 p50 ≤ 5 分・ハードタイムアウト 15 分（超過は kill → `failed`） |
| N2 | バンドルサイズ | ≤ 20MB（超過は `failed`） |
| N3 | 同時生成数 | ≤ 2（超過の generate は 409 detail「生成中のデモが多すぎる」。キュー化は将来 — YAGNI） |
| N4 | 可観測性 | 生成ログ（プロンプト・OpenCode の実行イベント・ビルド出力）をデモごとに保存（保存先は ADR。**秘密が混入しない入力構成 — F1 — が前提**）。`failed` の理由を `config.generation.error` に要約 |
| N5 | コスト記録 | 生成の LLM 使用量を usage_log の流儀で owner に紐づけて記録 |
| N6 | 再現性 | `config.frontend.generator = {model, prompt_version, opencode_version}` を記録（同一入力での再生成・障害解析の手掛かり） |
| N7 | リソース上限 | 生成環境に CPU / メモリ / ディスク / プロセス数の上限（値は ADR で実測確定）。**【人間承認済み緩和 2026-07-08・Internal 限定前提／ADR-0023】** PoC-first のため **CPU/メモリ上限 + 15 分ハードキル（N1）+ N2 バンドル検査**で足りるとし、**プロセス数/ディスク上限の強制方式は未検証のまま許容**（Container Instance の resource_config は CPU/メモリのみ）。顕在化時は OKE 移行（cgroup/quota）で対応 |

### 4.3 安全要件（must・fail-closed）

| # | 要件 |
|---|---|
| S1 | **隔離実行**: OpenCode の実行とビルドは生成ごとに使い捨てのサンドボックス内。ファイルシステムは作業ディレクトリに閉じ、**JetUse 本体のコード・設定・資格情報・DB 接続に到達できない**。ネットワーク egress は LLM エンドポイント（+ ADR が認めた最小限）のみ。方式（コンテナ分離・ネットワークポリシー等）は ADR |
| S2 | **資格情報ゼロ**: サンドボックスへ OCI 資格情報・DB ウォレット・.env を渡さない。LLM 認証（IAM 署名）は生成プロセスの外側で解く（署名プロキシ等 — ADR。生成プロセスが署名鍵を持たない構成を必須とする） |
| S3 | **スコープ担保（多層）**: (a) スキャフォールドの**固定 API クライアント**だけが HTTP を発行する構造（生成コードはブロック型 → クライアント呼び出しの形に誘導。ベースパスは相対 = 配信元の `/api/demos/{demo_id}/` に固定）。(b) **バンドル静的検査**（必須ゲート）: 全ファイルを走査し、絶対 URL（`http(s)://`）・プロトコル相対（`//`）・`/api/` 始まりのデモスコープ外パス・スキャフォールド外での `fetch(` / `XMLHttpRequest` / `WebSocket(` / `EventSource(` の生使用を検出したら**不合格 = failed**（許可リスト方式: 相対参照以外は原則落とす）。(c) **配信時 CSP**（§5.2）: `default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'` — 外部オリジンへの接続・読込をブラウザ側でも遮断 |
| S4 | **秘密混入の検査**: バンドル静的検査で秘密パターン（`ocid1.`・`-----BEGIN`・Bearer/JWT 風トークン・長エントロピー文字列の既知形式）を検出したら不合格。F1（入力に秘密を渡さない）と対の出口検査 |
| S5 | **原子的公開**: 生成は staging（`config.frontend` が指さない bundle_id）へ書き、**静的検査合格後に `config.frontend.bundle` のポインタ切替で公開**。検査不合格・途中失敗のバンドルが配信されることは構造的にない（配信は config 経由のみ — §5.2） |
| S6 | **箱の書き込み規律**: バンドル書き込み前に `demo_backend_targets`（kind='objectstorage'）へ write-ahead 記録（specs/18 §3.2 の既存台帳契約）。書き込みはリース保持区間で行う（§8.2） |

**脅威モデルの注記（正直な限定）**: 生成 SPA は利用者自身のブラウザ・権限で動くため、範囲外 API を
叩いても**権限昇格にはならない**。S3 の目的は (i) デモ体験がデモの箱に閉じる行儀の担保、
(ii) 秘密・データの外部送信の遮断（CSP が主）、(iii) SP4（マーケット配布 = 第三者コードの実行）で
必須になる検査体制の先取り、である。

**【人間承認済み緩和 2026-07-08・Internal 限定前提／ADR-0023】**（レビュー整合のための注記。要件本文は変更しない）:
- **S1（隔離実行のネットワーク面）**: 生成 CI から通常 API を到達不能にする**完全なネットワーク隔離
  （専用コールバック面の L3/L4 分離・ingress 制限・別立てブローカー）は PoC では要求しない**。Internal 限定
  前提でリスク許容し、**シンプルな到達構成**でよい（S2 の資格情報ゼロ・署名プロキシの最小権限 allowlist は維持）。
- **S3（スコープ担保）**: **自由 JSX 生成を継続**し、**同一オリジン配信によるデモ間 API 横断はリスク許容**
  （デモ別オリジン・opaque-origin sandbox は作らない）。ただし **(a) 固定 API クライアント・(b) バンドル静的検査・
  (c) 配信時 CSP の既存 3 層は仕様どおり維持**する。
- 顕在化時（マルチテナント公開・悪性利用）は **OKE 移行（NetworkPolicy/namespace 分離）+ audit 充実**で対応。

### 4.4 方式選定の委譲（SP3-03 の ADR）

次を **ADR（OpenCode 統合方式）**が確定する。予備比較は
docs/comparison/frontend-generation-runtime.md（定量欄は SP3-03 の実測で埋める）:

1. **ランタイム**: A: API コンテナ内 subprocess / B: 生成ごとの専用コンテナ（OKE Job 等）/
   C: 常駐ワーカー。**推奨仮説 = B**（S1 の隔離・N7 の上限がコンテナ境界で構造的に満たせる。
   起動レイテンシは N1 が分オーダーのため誤差）。ただし A の「隔離をプロセス+ネットワーク
   ポリシーで満たせるか」「B の Job 起動権限・イメージ配布の運用コスト」は実測・実機で判断。
2. **LLM 認証経路**: OpenCode の OpenAI 互換 provider 設定に IAM 署名をどう供給するか
   （署名プロキシ sidecar / 一時トークン等。S2 が枠）。**oci-genai-auth は openai-python 用で
   OpenCode には注入できない**ため、ここが技術リスクの核。ADR 前に実機検証する。
3. **egress とビルドのオフライン化**: 依存を焼き込んだスキャフォールド（vendored node_modules 等）で
   npm レジストリへの egress なしにビルドできるか。不能なら許可 egress の最小集合を定義。
4. **非同期の実行体**: FastAPI BackgroundTasks（追加部品ゼロ・API プロセス内）か、ランタイム B なら
   Job 起動 + 完了監視か。§4.5 の API 契約（202 + ポーリング）は方式によらず不変。
5. **N1/N7 の確定値**（実測）。
6. **OpenCode の headless 実挙動・生成品質**（プラン → 動く SPA の成功率。技術限界に当たったら
   実装を止め、findings を ADR / docs/verification/ に記録して人間判断 — STAGE3-PROGRESS の方針）。

### 4.5 生成オーケストレーション API（SP3-03。データ投入 §6 を含む束ね）

- `POST /api/builder/sessions/{sid}/generate`（`require_user` + 所有者）:
  前提 = `status='designed'` かつ plan あり（なければ 409）。
  body は任意 `{"model": <生成レジストリ key>}`（SP3-06 — §4.1 F2 の UI 切替）。
  省略（body なし含む）= 設定既定（`generation_model`）。**未知の model キー・未知フィールドは
  副作用（Demo 作成 / restart）前に 422**（fail-closed）。model の語彙 = **生成専用レジストリ**
  （`jetuse_core/gen_models.py` — 既定 gpt-oss-120b（自テナンシ）+ gpt-5 系 7 モデル
  （ORASEJAPAN 共有テナンシ。auth プロファイル / compartment は .env）。**共用 MODELS とは
  分離** — gpt-5 系をチャット UI へ漏らさない）。api 種別（chat/completions | responses）も
  レジストリが持ち、署名プロキシのルーティング・allowlist と単一真実源を共有する。
  使用モデルは既存の N6（`config.frontend.generator.model`）に記録。再実行（failed →
  restart）でも body の model が有効（未指定 = 設定既定であり「前回と同じ」ではない）。
  - 初回（`demo_id` null）: `Demo` を内部作成（name=plan.title / description=plan.description /
    `config.plan` = プラン / `status='provisioning'`）→ セッションに demo_id を記録 → **202**
    `{"demo_id": "..."}` を返し、非同期で ③a データ投入（§6）→ ③b フロント生成（§4）→
    ③c 静的検査 + 公開（§5）→ `ready`。
  - 再実行（demo が `failed`）: `set_status(failed→provisioning)` して同じ③を再実行（§1.3 の冪等上書き）。
    `provisioning`/`ready`/`deleting` 中の再実行は 409/404（deleting は存在秘匿 404）。
- 進行の確認 = `GET /api/demos/{demo_id}`（所有者は status を見る — specs/18 §2 既存）と
  `GET /api/builder/sessions/{sid}`（`demo_status` 添付 — §2.4）。ステップ粒度の進行表示が
  UI に必要なら `config.generation.step`（サーバ管理キー）を更新する（SP3-05 の要件次第。任意）。

## 5. 生成デモの保存・配信（SP3-03）

### 5.1 バンドルの保管

- 保管先 = **Object Storage**（jetuse-dev の loop 環境では Terraform 管理の専用バケットを再利用。
  **versioning は Disabled 必須** — specs/18 §3.2 3f と同じ削除保証の前提）。
- オブジェクト名 = `demo-bundles/<sha1(namespace) 40hex>/<bundle_id>/<相対パス>`。
  namespace 由来の完全ハッシュ prefix は specs/18 §3.1 の `file_key` と同じ導出規律
  （exact な削除根拠・長さ有界）。`bundle_id` = 生成ごとのサーバ生成 UUID。
- 再生成は新 bundle_id へ書いてポインタ切替（§4.3 S5）。**旧 bundle は切替成功後に削除**
  （失敗しても孤児は §5.4 の prefix 削除が回収するため best-effort でよい。ここだけは 503 にしない —
  切替済みで利用者影響がないため）。

### 5.2 配信 = API 経由（決定。PAR 直配信は不採用）

| 案 | 評価 |
|---|---|
| **A: API 経由配信（採用）** `GET /api/demos/{demo_id}/app/{path}` | `require_demo`（+ ready ゲート §8.1）が構造的に効く = 認可・存在秘匿が demos 契約と同一面。CSP 等の応答ヘッダを自前で制御できる。バンドルは ≤20MB・静的アセットの逐次取得でありゲートウェイ経由のストリームで十分 |
| B: PAR（事前認証リクエスト）直配信 | 認可が URL 所持に化ける（存在秘匿・visibility 変更・削除との整合が壊れる）。期限・失効の管理部品が増える。CSP を Object Storage 側で制御できない。**不採用** |

- ルート: `GET /api/demos/{demo_id}/app/{path:path}`（`require_demo` + `status='ready'`）。
  `path` 空・`/` 終端・拡張子なしパスは `index.html` を返す（SPA のディープリンクは v1 非対応で可 —
  生成 SPA は単一エントリ前提）。
- **パス検証（信頼境界）**: `config.frontend.bundle` は UUID 形式のみ許可（サーバが
  `demo-bundles/<sha1(namespace)>/` を導出して結合 — config 値でバケット横断させない）。
  `path` は正規化して `..`・絶対パス・バックスラッシュを 404（トラバーサル防止）。
- 応答ヘッダ: §4.3 S3(c) の CSP・`X-Content-Type-Options: nosniff`・
  `Cache-Control: private, max-age=3600`（bundle_id が変わるため長期キャッシュ可だが private）。
  Content-Type は拡張子の許可リスト（html/js/css/json/svg/png/jpg/webp/woff2/map）で決め、
  リスト外は `application/octet-stream`。
- 404 系: バンドル未公開（`config.frontend` なし）・オブジェクト不存在は 404
  （ready ゲートにより通常は起きない — 防御的）。

### 5.3 Demo.config の正規キー拡張（specs/18 §2.2 の追補）

SP2 の「config は不透明・`config.dbchat.model` のみ正規」を次の通り拡張する:

| キー | 所有 | PATCH 可否 |
|---|---|---|
| `config.dbchat.model` | owner（既存 — specs/18 §2.2 の検証のまま） | 可 |
| `config.plan` | **サーバ管理**（§4.5 の生成開始時に設定。生成物・投入データの正） | **不可** — PATCH 入力に含まれたら 422 |
| `config.frontend` | **サーバ管理**（`{bundle, entry, generated_at, generator}` — §4.2 N6・§4.3 S5） | **不可** — 同上 |
| `config.generation` | **サーバ管理**（`{error, step}` — §1.2・§4.5） | **不可** — 同上 |

- PATCH の config は全置換（SP2 実装）のため、**サーバ管理キーはサーバ側で現行値を温存して
  マージする**（入力に含まれていれば 422、含まれていなければ現行値を保つ）。ビルダー産でない
  demo（config にサーバ管理キーなし）の挙動は不変。境界テスト: config 全置換 PATCH で
  plan/frontend が消えない・入力に混ぜたら 422。

### 5.4 後始末（specs/18 §3.2 への追記 — SP3-03 で実装）

- DELETE の手順 3 に **3g** を加える: **バンドル prefix `demo-bundles/<sha1(namespace)>/` を
  ページネーション完走で全列挙して削除**（NotFound 以外の失敗は 503 — 中断・再 DELETE で収束）。
  スキップ判定の正は台帳（kind='objectstorage' の行 — specs/18 §3.2 の既存契約に載る）。
  versioning Disabled の preflight も既存契約と同一。
- staging バンドル（未公開 bundle_id）も同 prefix 配下にあり、3g が一括回収する
  （生成側の補償削除は不要 — §1.3）。
- E2E 事後条件: DELETE 後に当該 prefix のオブジェクトがゼロ。

## 6. サンプルデータ生成・投入（SP3-04）

### 6.1 表データ（plan.data.tables → datasets 機構）

- 投入先 = **SP2 の箱**そのもの: `create_dataset(namespace='demo_<id>', …)` 系の内部関数を直接使う
  （registry-first・VPD 付与・完全ハッシュ命名・排他リース・箱あたり上限 — specs/18 §3.1・§3.2 の
  既存契約が全部そのまま効く。**新しい投入経路・命名・後始末部品を作らない**）。
- 行データ生成 = LLM（既存の `generate_dataset` サンプル生成系があれば流用を第一候補にする —
  実装判断。プラン列定義 + 業種文脈からもっともらしい行を生成）。**件数・型はプラン準拠**:
  生成行はサーバ側で型・件数を検証し、不足/不一致は有界再試行 → だめなら生成失敗（→ `failed`）。
  乱数由来の多様性は行値にのみ許す（スキーマは §3.3 で固定済み）。
- dbchat 整合: 投入後は SP2-03 のデモスコープ dbchat（`/api/demos/{id}/dbchat/*`）が
  そのまま引ける（DemoContext.namespace 解決 — 追加実装なし）。`config.dbchat.model` は
  プランでは指定しない（既定モデル。変更は owner の PATCH — specs/18 §4.3 既存契約）。

### 6.2 文書（plan.data.documents → デモ RAG 箱）

- LLM がプランの `outline`・業種文脈から Markdown 本文を生成し、**デモ RAG upload の内部関数**
  （ensure_store の lazy 生成・予約 ledger・quota・原本 put — specs/18 §3.1 の既存経路）で投入する。
  初回は store 作成 40〜60 秒（SP1-03 実測）を生成時間 N1 に織り込む。
- 文書サイズ上限: 1 文書 ≤ 64KB（LLM 出力の有界化。超過は再試行 → 失敗）。

### 6.3 冪等性（再生成 = 置換）

- 同名 dataset は「既存削除（外部先行順の delete_dataset — specs/18 §3.2 手順 2 改修済み）→
  再作成」。同名文書は「既存 rag ファイル削除（外部先行 — specs/18 §3.2 の個別削除契約）→
  再 upload」。→ §1.3 の「failed → 再生成で収束」が成立。
- 途中失敗の残骸（登録済み dataset・upload 済み文書）は次回再生成の置換 or DELETE の後始末が
  回収する（本タスク固有の掃除部品なし）。

### 6.4 隔離・後始末（既存契約の確認のみ）

- 他デモ・共有スキーマ（SH 等）・user 資産への不干渉は、既存の namespace キー分離 + VPD +
  完全ハッシュ命名（specs/18 §3〜§4）が構造的に担保する。本タスクは owner キーに
  `ctx.namespace` 以外を渡さないこと（レビュー観点）。
- demo DELETE で本タスク由来の表・文書・store が全て消えるのは specs/18 §3.2 の既存後始末
  （E2E で確認 — §9 SP3-04）。

## 7. ビルダー UI（SP3-05）

- フロー（1 画面のウィザード。Internal 面 — AUTH_REQUIRED=true 前提の配備で提供）:
  1. **ヒアリング**: NL チャット UI（§2.4 messages）。requirements の充足状況（§2.2 の必須項目）を
     サイドに可視化。`sufficient=true` で「設計へ」を活性化。
  2. **プラン確認**: §3.1 design を実行し、プランを**要約表示**（画面構成・能力・データ定義）。
     **プラン JSON の直接編集はさせない**（スキーマ破壊の温床）。修正は追加発話 → 再 design
     のループ（§3.1 の再実行契約）。タイトル・説明のみ直接編集可（プランに反映して再検証）。
  3. **生成**: §4.5 generate → 202。進行はポーリング（`GET /api/builder/sessions/{sid}` の
     demo_status。§4.5 の `config.generation.step` を出すかは実装時の UX 判断）。
     `failed` は理由（`config.generation.error`）と「再生成」ボタンを表示。
  4. **プレビュー**: `ready` 後、生成デモ `/api/demos/{demo_id}/app/` を新タブ（または iframe —
     CSP と認証ヘッダの取り回しで実装時に選ぶ）で開く。
  5. **確定**: name/description の最終編集（PATCH — SP2 CRUD）。「破棄」= DELETE（確認ダイアログ付き。
     specs/18 の後始末が走る）。確定後、既存の demos 一覧に現れる（SP2 CRUD — 追加実装なし）。
- 直近セッションの復帰: v1 は localStorage にセッション id を保持（一覧 API は residual — §2.4）。
- 既存 SPA のデザイントークン・コンポーネント流儀（UI-02 等）に準拠。`npm run build` 成功が完了条件。

## 8. 横断: 認可・状態・既存契約との整合

### 8.1 ready ゲート（require_demo の一般化 — SP3-01 で導入）

- **デモスコープ能力ルート（chat / rag / dbchat / conversations）と `/app/` 配信は
  `status='ready'` のみ通す**。`provisioning` / `failed` / `deleting` は既存の存在秘匿と同じ
  **404**（specs/18 §2.3 の deleting 404 を「ready 以外 404」へ一般化。生成中の箱への外部
  アクセスが lazy 生成と競合する余地を構造的に消す）。
- demos CRUD メタ（GET / PATCH / DELETE）は従来どおり（所有者は provisioning/failed でも
  status を見られる — 進行表示・再生成・破棄に必要）。
- SP2 までに非 ready の demo を作る経路は存在しないため、**この一般化による既存挙動の変化はない**
  （回帰テストで担保）。

### 8.2 排他リースとの整合（specs/18 §3.2.1 の適用）

- 生成ジョブの**箱に書く区間**（③a の dataset/RAG 投入・③c のバンドル書き込みと台帳 upsert・
  config/status 更新）は demo 排他リースを保持して行う（最外層取得・内部伝播の再入契約は既存のまま）。
- **LLM 呼び出し・OpenCode 実行（③b）はリースを跨がない**（分オーダーの保持は DELETE を
  不当に待たせる — specs/18 §3.2.1 の「SSE 本体は跨がない」と同じ原則）。③b の成果物は
  サンドボックス内のローカル成果であり、箱への反映（③c）だけをリース区間にする。
- フェーズ境界の status 再確認（§1.2）とリースで、「生成 vs DELETE」の競合は
  既存契約の範囲に収まる（新しい競合部品なし）。

### 8.3 既存契約への影響なし（確認リスト）

- **conversations**: 生成フロントの chat は**会話を保存しない**（conversation_id 不使用・
  ブラウザ内メモリ履歴のみ）。specs/18 §4.2 が SP3 送りにした「デモ会話の一覧・履歴・個別削除
  API」は**本仕様でも追加しない**（生成デモの要件に無い — YAGNI。必要なデモ類型が出たら residual）。
- **LTM**: demo chat の LTM は無効のまま（specs/18 §4.2）。合成 subject の設計は行わない（residual）。
- **agents の owner_key 伝播**（specs/18 §4.3 (3) が「SP3 の課題」とした件): agents は
  プラン語彙外（§3.4 — デモスコープルートなし）のため **SP3 では不要・非対応を明示**する。
  agents のデモスコープ化（語彙拡張）とセットで将来設計する（residual）。
- **capabilities カタログ**: ビルダー API（`/api/builder/*`）と `/app/` 配信は**裏方**であり
  カタログに載せない（specs/17 §4 の区分。デモが合成する能力ではない）。
- **usage_log**: ヒアリング・設計・生成の LLM 使用は実ユーザー（owner）に紐づけて記録
  （specs/18 §3.2 手順 4 の「監査は人に紐づける」原則と同じ）。

## 9. 受け入れ条件（タスク別 — tasks/SP3-0X.md の確定版と同一）

実環境 E2E は**デプロイ済みプレビュー環境**（STAGE3-PROGRESS「E2E 方針」— RM スタック
`jetuse-dev-app` / AUTH オフのため owner 検証は dev-user・複数ユーザー検証は fake/単体で補完）に
対して行い、証跡を `runs/<run-id>/e2e/` に残す。

- **SP3-01**: builder_sessions migration（§2.1 単文 2 ファイル）が fresh 適用・冪等再適用とも成功
  （ランナーの事後条件検証対象に追加）。§2.4 の 3 ルートが存在し、単体テスト（fake LLM）で
  ヒアリング往復（不足 → 追質問 / 充足 → sufficient=true）・**サーバ側決定的再検査**
  （LLM が sufficient=true でも必須欠落なら false）・越境 404（存在秘匿の同一形）・401・
  transcript/発話の上限 422・demo_id 設定後の読み取り専用 409 を検証。
  §8.1 の ready ゲート（能力ルート・app 配信の「ready 以外 404」共通依存）を導入し、
  既存 demo ルートの回帰なし（SP2 テスト全緑）・ruff クリーン。
  実環境 E2E: プレビュー環境 + 実 LLM で 1 セッションの往復 → sufficient=true 到達を記録。
- **SP3-02**: `design` ルート（§3.1 — 409 前提条件・有界再試行・上書き再実行）と
  プランスキーマ（§3.2・§3.3）が存在し、単体テスト（fake LLM）で 語彙外能力 422・
  語彙の構造的導出（カタログの demo スコープ routes から — §3.4）・識別子/型/上限の境界・
  データ整合（能力⇔データ定義）・再試行での合格・スナップショットを検証。ruff クリーン・回帰なし。
  実環境 E2E: プレビュー環境の実カタログ + 実 LLM で要求サマリ → 検証合格プラン産出を記録。
- **SP3-03**: **ADR（OpenCode 統合方式 — §4.4 の 6 項目）が人間承認済み**（adr_approval ゲート。
  承認前に実装着手しない）。§4.5 の generate（202 + 非同期・再実行契約・フェーズ境界の status
  再確認・リース区間 §8.2）、§4.3 の安全ゲート（静的検査: スコープ外参照・生 fetch・秘密パターンの
  fixture バンドルで不合格 = failed を単体検証）、§5 の保管（staging → ポインタ切替の原子公開・
  台帳 write-ahead）と配信（require_demo + ready・パス検証/トラバーサル 404・CSP/nosniff ヘッダ・
  Content-Type 許可リスト）、§5.3 の config サーバ管理キー（PATCH 422 / 温存マージ）、
  §5.4 の後始末 3g（prefix 完全削除・503 収束）を実装。単体テスト全緑・ruff クリーン・回帰なし。
  実環境 E2E: プレビュー環境で プラン 1 件 → 生成 → `/app/` 配信 → ブラウザ疎通（生成デモから
  chat/rag が実応答）・DELETE で prefix ゼロ。**技術限界に当たったら実装を止め、findings を
  ADR / docs/verification/SP3-03.md に記録して人間判断を仰ぐ**。
- **SP3-04**: §6 の投入（datasets/RAG とも**既存内部関数のみ**で投入 — 新規経路なし）・
  プラン準拠の型/件数検証・§6.3 の冪等置換を実装。単体テスト（fake LLM / fake 外部）で
  型不一致の再試行 → 失敗・同名置換・owner キーが ctx.namespace 固定であることを検証。
  ruff クリーン・回帰なし。実環境 E2E: プレビュー環境で プラン → `demo_<id>` へ表 + 文書投入 →
  デモスコープ dbchat の日本語照会が投入データで答える → rag.search が投入文書を引く →
  demo DELETE で表・文書・store・バンドルが全て消える（specs/18 §3.2 + §5.4 の事後条件）。
- **SP3-05**: §7 のフロー（ヒアリング → プラン確認 → 生成進行 → プレビュー → 確定/破棄）が
  ビルダー UI として動き、保存後に demos 一覧（SP2 CRUD)へ現れる。`npm run build` 緑・
  UI 単体テスト（状態遷移・エラー表示・failed の再生成導線）緑・既存 web 回帰なし。
  **ステージ総合 E2E**: プレビュー環境でビルダー UI から新規デモを 1 件作り切り
  （ヒアリング → 設計 → 生成 → データ投入 → ready → 生成デモが `/app/` で開き chat/rag/dbchat を
  デモスコープで実行）、旧 UC-03「非開発者が 5 分でデモ作成」相当を実機確認・記録。

## 10. ステージ完了条件（STAGE3-PROGRESS.md と同一）

- specs/19（本仕様）が人間承認済み。全タスク Codex review PASS・test/lint クリーン・
  実環境 E2E（または理由付き SKIPPED）通過。
- プレビュー環境で「フィールドSA がヒアリングに答える → ビルダーが能力カタログからデモを設計 →
  OpenCode + OCI モデルで静的SPA を生成 → サンプルデータを `demo_<id>` に投入 → `Demo` として
  保存 → `/api/demos/{id}/app/` で生成デモが開き、JetUse API（chat/rag/dbchat）をデモスコープで
  叩ける」の一連が通る（§9 SP3-05 の総合 E2E）。
- 生成フロントはバックエンドを生成しない（JetUse API を叩くだけ）。生成物の安全性
  （§4.3 — スコープ外を叩けない・秘密を埋め込まない）が構造的に担保される。
- `dev` が常時デプロイ可能（main 由来機能・SP2 の回帰なし。既存テスト・`npm run build` 緑）。

## 11. 非ゴール

- マーケットプレイス（SP4）: 公開・配布・署名・第三者コード審査。`visibility='published'`。
- `connector.invoke`（外部接続能力）とそれを使うデモ。
- **プラン語彙の拡張**（stateless 系能力のデモスコープパススルー — §3.4 案 B。レシピは
  specs/18 §4.3 どおり「ルート + ディスクリプタ追記」。必要になった時点で追加 — residual）。
- agents の owner_key 伝播・デモスコープ化（§8.3）。demo chat の LTM 有効化（合成 subject 設計）。
- デモ会話の永続化・一覧/履歴 API（§8.3 — 生成フロントは会話を保存しない）。
- プラン JSON の自由編集 UI（§7 — 修正は追加発話 → 再設計）。生成物への人手パッチ運用。
- IaC 生成（ADR-0015 決定 6）。デモ専用サーバコードの生成（同 決定 2・3）。
- Public（main）の user 単位ルート・usecases の挙動変更。

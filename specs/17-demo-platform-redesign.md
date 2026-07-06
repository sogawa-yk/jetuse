# specs/17: JetUse デモ生成プラットフォーム 再設計 — SP1: JetUse API

> 状態: ドラフト（人間レビュー待ち）。日付: 2026-07-06。
> 設計判断: `docs/decisions/ADR-0015`（本再設計。ADR-0013 を置換）・`docs/decisions/ADR-0016`（ブランチ）。
> 本仕様は **SP1（JetUse API）** を詳細化し、SP2〜4 は分解と役割分担のみ概略で添える。

## 0. 位置づけ・背景

JetUse を「フィールドSAが、リファレンスアーキテクチャから外れずに、顧客業務に寄り添ったデモを短時間で作れる」
プラットフォームへ拡張する。2026-07-05 のリポジトリ方針転換（main のみへリセット）を受け、過去の
デモ生成プラットフォーム設計（ADR-0013 / specs/16）に縛られず**フレッシュに再設計する**（決定=C）。
main に生存する資産（認証コンテキスト・usecases の owner/visibility・manifest 署名・file_search/ベクタストア・
サンプルスキーマ実体化）は、合う所だけ日和見的に流用する（アーキの縛りにはしない）。

**2つの版**（`docs/guides/branching-and-releases.md` / ADR-0014・0016）:
- **Public 版**: 各ユーザーが自環境へセルフホスト。OCI の AI 機能を気軽に試すショーケース。`main` 配信。
- **Internal 版**: ベンダー（施主）が単一インスタンスをホスティングし、フィールドSA が Identity Domains
  認証でアクセス。ビルダー／マーケットプレイス／マルチテナントを上に重ねる。`internal-stable` 配信。

## 1. 全体像（サブプロジェクト分解）

各 SP は独立した 仕様→計画→実装 サイクルを持つ。

| # | サブプロジェクト | 内容 | 版 | 開発枝（merge先） |
|---|---|---|---|---|
| **SP1** | **JetUse API** | 既存能力を「デモ生成フロントが叩ける安定API面」に整理。能力追加を安く保つ。 | 共通 | `main`（→ sync `dev`） |
| SP2 | テナンシ + Demo エンティティ | Identity Domains でユーザー分離。`Demo`(owner/visibility) を一級化。デモ単位のデータ箱を生成。 | Internal | `dev` |
| SP3 | ビルダー | ヒアリング(NL)→能力の選択/配線→フロント生成(OpenCode + OCIモデル)→データ生成→Demo 産出。 | Internal | `dev` |
| SP4 | マーケットプレイス | 公開/配布。SP2 でデータモデルに `visibility` を仕込み後付け。 | Internal（将来） | `dev` |

> 「開発枝」は各 SP の作業を merge する先であり、**配信元ではない**（§7 / ADR-0016）。配信元は Public=`main`
> （tag `public-vX.Y.Z`）、Internal=`internal-stable`（`dev → internal-stable` リリース + tag `internal-vX.Y.Z`）。
> Internal 固有の SP2〜4 は `dev` に積み、リリース点で `internal-stable` へ落として本番配信する。

**基本方針の確定事項（全 SP 共通）**:
- 能力モデル: **既存 JetUse 能力の組み合わせのみ**。生成するのは**フロント + データ**。バックエンドは JetUse 固定。
- 実行時のデモ = **静的SPAバンドル**（デモごと）。JetUse が `/api/demos/{id}/...` 配下で配信し、ブラウザから
  ユーザー認証 + デモスコープで JetUse API を叩く。**デモ専用のサーバコードは持たない**。
- 秘密・外部接続は**デモ専用サーバではなく共有の `connector.invoke` 能力**で解く（秘密は Vault、サーバ側で
  JetUse が代理呼び出し）。デモ専用フル app（コンテナ）は将来の限定的エスケープハッチとしてのみ留保。

## 2. SP1 スコープ

SP1 = 下流（SP2/SP3）を動かすために JetUse API が提供すべき 3 要素。**既存ルートの全面書き換えではなく、
「どれを能力として公開し、どう記述し、どうデモ単位にスコープするか」の整理が本体**。

1. **能力カタログ** — ビルダーが読む機械可読な「メニュー表」。
2. **デモ向け安定API面** — どの既存ルートを「デモ合成可能な能力」として公開するかの確定。
3. **(user, demo) スコープの継ぎ目** — 呼び出しにデモが乗り、データがデモ単位に分離される seam。

## 3. 能力カタログ（要素1）

**方式 = 案1: 自動 OpenAPI + 手書きディスクリプタ**。

- FastAPI が自動生成する `/openapi.json`（技術契約: path/method/入出力スキーマ）を土台にする。
- その上に、デモ向け能力にだけ **手書きの能力ディスクリプタ**を1件持つ。フィールド:
  - `capability`（例 `rag.search`）／`summary`（何ができる）／`when_to_use`（デモでの使いどころ）／
    `example`（入力例→出力の要点）／`demo_safe`（デモ合成に出してよいか）／`route`（対応する OpenAPI path）。
- ビルダー(SP3)へは「OpenAPI（技術詳細）＋ ディスクリプタ（用途・例・安全フラグ）」を統合したカタログを返す
  1 エンドポイント（例 `GET /api/capabilities`）で提供する。カタログの**出力形は将来も不変**に保つ。
- **能力追加のコスト = ルート追加 + ディスクリプタ1件**。

**将来の移行**: 能力が増えてディスクリプタの書式がブレ、ビルダーの生成品質が落ち始めたら、統一 Capability
インターフェース（各能力が metadata+入出力schema+invoke を実装しレジストリが自動カタログ化する「案2」）へ
寄せる。カタログの出力形が不変なので **SP3 は無改修**（内部の作り方だけ差し替え）。

## 4. 公開する能力（要素2）

**デモ向け能力（カタログに `demo_safe=true` で載せる = 生成フロントが叩ける）**:

| 能力 | 内容 | 既存ルート |
|---|---|---|
| `chat` | LLM 対話（ストリーミング） | `routes/chat.py` |
| `rag.search` | 文書検索Q&A（引用付き） | `routes/rag.py` |
| `dbchat` | 自然言語→SQL でデータ照会 | `routes/dbchat.py` |
| `agents` | エージェント/ツール実行 | `routes/agents.py` |
| `voice` | STT/TTS・文字起こし | `routes/voice.py` |
| `minutes` | 議事録（文字起こし+要約） | `routes/minutes.py` |
| `translate` | 翻訳 | `jetuse_core/translate.py` |
| `docunderstand` | 文書理解・抽出 | `jetuse_core/docunderstand.py` |

**裏方（カタログに載せない）**: admin / conversations（履歴CRUD）/ tools / mcp_servers / datasets / embeddings /
moderation / guardrails（自動適用の横断機能）。

**将来足す能力**: `connector.invoke`（秘密・外部接続。顧客業務デモの説得力の要。早期に1本）／
`demo データのプロビジョニング`（デモ専用スキーマ作成 + データ投入。SP3 のデータ生成の着地先・SP2 寄り）。

**既存 usecases の扱い = A（存続）**: usecases（fields+template の自作ミニアプリ）は **Public 版のショーケース
機能として存続**する。Internal のビルダーとはペルソナ・用途が別物であり、統合しない。

## 5. (user, demo) スコープの継ぎ目（要素3）

**`DemoContext` seam**: 呼び出しのたびに共有の依存関数が
1. `demo_id` を受け取り、2. **認証ユーザーがそのデモの所有者か（or 公開済みか）を検証**し、
3. デモの箱の実体（DBスキーマ名・RAGストアID・会話名前空間等）を束ねた `DemoContext` を返す。

各能力は生の `user` ではなく `DemoContext` を受け取り、**その箱の中だけ**を操作する。所有権検証を通らない
呼び出しは 404/403 で弾く（データ分離は信頼境界。fail-closed）。

**箱の分け方（既存資産流用）**:
- RAG → デモごとに別ベクタストア（file_search をデモ id で名前空間分け）。
- DB → デモごとに別スキーマ `demo_<id>`（サンプルスキーマ実体化の仕組みを流用）。
- 会話 → `demo_id` で紐付け。

**demo_id の渡し方 = パス**: `/api/demos/{demo_id}/rag/search` のようにパスへ含める（**付け忘れ防止**＝
スコープ漏れをルーティングで構造的に防ぐ）。Public 用の user 単位ルート（`/api/rag/search` 等）は現状のまま
共存させる。

**役割分担**: SP1 は **継ぎ目（`DemoContext` 依存関数と、能力がそれを受け取る形）**まで敷く。**実際の Demo
エンティティの保存・箱のプロビジョニングは SP2**。

## 6. SP2〜4 概略（本仕様の詳細対象外）

- **SP2**: `Demo`(id, owner_sub, visibility, name, config, created_at…) を DB に保存（usecases の owner/visibility
  パターンを踏襲）。デモ作成時に箱（スキーマ・ベクタストア）をプロビジョニング。`DemoContext` の解決先を実装。
- **SP3**: ヒアリング → 能力カタログを LLM に渡してデモ設計 → OpenCode + OCI モデルで静的SPA生成 →
  サンプルデータ生成・投入 → `Demo` として保存。生成フロントは JetUse API を叩くだけ（バックエンド生成なし）。
- **SP4**: 中央レジストリ + 署名（manifest 署名の既存実装を土台）。`visibility=published` のデモを配布。

## 7. ブランチ / リリース

3 長期ブランチ（詳細は `docs/guides/branching-and-releases.md` / ADR-0016）:
- `main` = Public 安定版・Deploy ボタン配布元（常時デプロイ可）。
- `dev` = Internal 統合（開発）。`main ⊆ dev`。
- `internal-stable` = Internal 安定版（新設）。施主のホスト本番が追う。`dev → internal-stable` でリリース。

SP1 は `main` 発（Public flow）→ `dev` へ sync。SP2〜4 は `dev` 発。

## 8. 非ゴール（SP1）

- Demo エンティティの保存・プロビジョニング（SP2）。ビルダー（SP3）。マーケットプレイス（SP4）。
- `connector.invoke` の実装（将来能力）。統一 Capability インターフェース（案2 は将来移行）。
- 既存能力の内部ロジックの作り替え（SP1 は公開面・カタログ・seam の整理に限定）。

## 9. 受け入れ条件（SP1 完了ゲート）

- `GET /api/capabilities` が 8 能力のカタログ（OpenAPI 由来の技術詳細 + 手書きディスクリプタ）を返す。
- 8 能力それぞれに `demo_safe=true` のディスクリプタが存在し、裏方ルートは載らない。
- `/api/demos/{demo_id}/...` 配下の能力が `DemoContext` を経由し、**他ユーザーのデモ id では 404/403** に
  なる（所有権検証の実機/テスト確認）。Public 用 user 単位ルートは従来どおり動作。
- 既存の Public 機能（usecases 含む）が回帰なく動き、`main` が常時デプロイ可能を維持。
- area の test/lint 緑 + 実環境 E2E（能力カタログ取得 + デモスコープ越境拒否）通過。

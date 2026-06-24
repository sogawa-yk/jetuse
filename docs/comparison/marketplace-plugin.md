# 比較: プラグイン機構 / マーケットプレイス（JetUse プラットフォーム化）

エンハンス案 `docs/enhance/202607.md` の実現方式比較。
狙い = JetUse を「OCIマネージドサービスを使ったAIユースケース**集**アプリ」から
「AIユースケース／エージェント**開発プラットフォーム**」へ拡張する際の選択肢整理。

> 本ドキュメントは**設計検討段階**（未実装・未検証）。状態列の「提案」は今後ADR化・PoC対象。
> 実機検証後に各方式の状態を更新する。

関連: [ADR-0009 エージェント・マルチフレームワーク]・`docs/comparison/agent-frameworks.md`・
`docs/comparison/rag-backends.md`・`docs/comparison/agent-runtimes.md`

---

## 0. 前提整理: エンハンス案には4種類の「プラグイン」が混在

提案を成立させる鍵は、プラグインを**ティアに分けて**それぞれ別の仕組みに割り当てること。
現状JetUseの拡張シーム（設定駆動=コード改修不要 / ハードコード=要改修）:

| 拡張対象 | 現状 | 該当コード | MP化の難度 |
|---|---|---|---|
| ユースケース（テンプレ+フォーム） | ✅ 設定駆動（ADBにJSON定義、`/builder`） | `jetuse_core/usecases.py`, `service/schemas.py:150` | 低 |
| エージェント定義 | ✅ 設定駆動（4フレームワークdispatch） | `jetuse_core/agents.py`, `service/agent_dispatch.py` | 低 |
| ツール | ⚠️ MCPは動的登録可 / 組込ツールはPythonハードコード | `jetuse_core/mcp_servers.py`(動的), `jetuse_core/tools.py`(静的) | 中 |
| エージェントループ | ✅ select_ai/openai_agents/langgraph/adk/native を既にdispatch | `service/agent_dispatch.py` | 低〜中 |
| ナレッジベース(RAG) | ⚠️ vector_store/select_ai/opensearch を `if文`で切替（抽象化なし） | `jetuse_core/rag*.py`, `routes/chat.py` | 中 |
| モデル / 画面ルート / ナビ | ❌ 全てハードコード | `models.py`, `web/src/App.tsx`, `components/layout.tsx` | 高 |

**プラグインの4ティア**（これを分けないとプラグイン機構が無限に肥大化する）:

- **Tier A — 宣言データ**（usecase / agent定義）: 既に設定駆動。MP=JSON定義の配布で済む。**最小工数で最大効果**。
- **Tier B — ツール／コネクタ**（実行コード伴う）: **MCPサーバに正規化**するのが本命（既存MCPレジストリ+SSRFガードの延長）。
- **Tier C — ナレッジベース／データソース**: RAGの`if文`を Provider Interface に抽象化し登録可能リソース化。
- **Tier D — フルアプリ**（独自UI付き）: JetUse本体には載らない。別コンテナ連携 or 機能再実装の二択（§4）。

**線引き方針**: A/B/C を正式サポート、D は限定的な外部連携に留める。

---

## 1. 決定軸①: プラグイン実行・隔離モデル

マーケットプレイス = 「他者のコードを自テナンシで動かす」ため隔離が核心。
CLAUDE.mdのセキュリティ姿勢（SSRFガード・owner_sub分離・承認フロー）と整合させる。

| 方式 | 隔離 | 工数 | 既存資産流用 | 評価 |
|---|---|---|---|---|
| **A. MCPサーバ（コンテナ）化（採用候補）** | プロセス/ネットワーク分離◎ | 中 | MCPレジストリ・`packages/agent-containers`・hosted-agent | **本命**。信頼境界が明確、ツールMPと一致 |
| B. In-process Python動的import（entry points） | ❌なし | 低 | `tools.py`拡張 | 速いがMPには危険。**署名済み公式プラグイン限定**なら可 |
| C. Hostedコンテナ（ADR-0009方式）単位 | ◎ | 中〜高 | `packages/hosted-agent-sample` | フルアプリ系（Tier D）に向く |
| D. WASM/サンドボックス | ◎ | 高 | なし | OCIにマネージドWASM基盤なし、オーバーキル。不採用 |
| E. フロント Module Federation（マイクロフロントエンド） | UI分離 | 高 | なし | 独自UIプラグインが要る場合のみ（Tier D後段） |

- **推奨**: 実行コードを伴うプラグインは原則 **MCPサーバ（OCI Container Instances / OCIR配布）に正規化**。
  提案8行目「ツールの実装もマーケットプレイスから取得」が既存MCP機構の延長で実現でき、隔離も担保。
- In-process(B) は起動レイテンシ0だが信頼境界がない → **公式署名プラグインのみ**に限定する運用とセット。

---

## 2. 決定軸②: クロスインスタンス・マーケットプレイス（レジストリ）

「公開物を全JetUseインスタンスから参照」(提案12行目) = **中央レジストリ**が新規に必要。
現状は各instanceがADBにowner_sub分離でローカル保存 → MPは別コンポーネント。

| 方式 | 構成 | コスト | バージョン/レビュー/署名 | 評価 |
|---|---|---|---|---|
| **A. Object Storage + マニフェストindex（PoC採用候補）** | 共有バケット+PAR、`index.json`+成果物 | 最小 | 自前実装 | **PoC〜初期に最適**。即立つ |
| **B. 専用レジストリμService + ADB（本番本命）** | 公開/検索/install API、評価・DL数・バージョン | 中 | フル制御 | **本番の本命** |
| C. OCIR（コンテナレジストリ）+ index | コンテナ系プラグイン（Tier B/D）に最適 | 中 | OCIR署名活用 | A/Bと**併用** |
| D. Git-based（Backstage / Claude plugins型） | マニフェストをGitリポジトリ管理、各instanceがpull | 低 | Git PRがレビュー兼ねる | エンプラ配布に親和的 |

**横断で必須の論点**（MP=コード配布の宿命）:

1. **マニフェスト・スキーマ**: `type(usecase|agent|tool|kb|app)` / `runtime` / 要求権限・データソース・モデル / `version` / **署名**。
2. **信頼・ガバナンス**: 署名検証、**公式 / コミュニティ / プライベート**の3層、セキュリティレビュー必須（実機検証主義と整合）。
3. **テナント分離**: 公開はメタデータのみ。実体（ADB接続・Vault Secret）は各instanceローカルに留め、プラグインには**最小権限**だけ渡す。

---

## 3. 決定軸③: 既存資産3つの取り込み方式

3資産はいずれも**独立した別アプリ**（UIフレームワークもバックエンドも別）→ そのままReact+FastAPIには載らない。
「既存資産の書き換えが必要？」(提案3行目)の答えは **Yes**。どのレベルで取り込むかが選択肢。

| 資産 | UI | バックエンド | JetUseとの機能重複 |
|---|---|---|---|
| No.1-RAG | Gradio 5 | Python + ADB + LangChain vector | RAGと**重複**（高機能: Vision QA・全文検索・リランク） |
| No.1-SQL-Assist | Gradio | Python + ADB Select AI | DbChat/NL2SQLと**重複** |
| 伝ぴょん(denpyo-toroku-kun) | Oracle JET 16 | Flask + VLM-OCR + ADB + Object Storage | **重複なし**（請求書登録ワークフロー） |

| 取り込み方式 | 内容 | 工数 | 向く資産 |
|---|---|---|---|
| **① 外部アプリ連携（iframe/リンク+SSO）** | 各アプリをOCI Container Instancesで別稼働、JetUseは「アプリカード」で起動 | 小 | **伝ぴょん**（独自JET UIをそのまま活かす） |
| **② MCPツール化（UIを捨て中身だけ）** | 例: No.1-RAGの検索パイプラインをMCPサーバ化し、JetUseエージェントから呼ぶ | 中 | **No.1-RAG / SQL-Assist** |
| ③ JetUseプリミティブに再実装 | 機能をusecase/agent/RAGバックエンドとして作り直す | 大 | 重複が許容できないもの |

- **推奨**: No.1-RAG・SQL-Assist → 機能重複のため **②MCPツール化**で吸収（決定軸①のMCP正規化と一致、Gradio UIは捨てる）。
  伝ぴょん → 重複なし・独自業務UI → **①外部アプリ連携**で“載せる”（書き換え最小）。将来②/③へ。
- 方針: **フルアプリを無理にプラグイン化しない**（Tier D = 外部連携に留める）。

---

## 4. 決定軸④: エージェント作成・ループ選択（提案7-10行目）

既存資産（ADR-0009のframework dispatch）が一番効く領域。UIに露出させるだけで大きく進む。

| 項目 | 最小選択肢 | 拡張選択肢 |
|---|---|---|
| ループ選択(提案9) | `framework`(select_ai/openai_agents/langgraph/adk/native)を**AgentBuilderで選択可能化**（小） | ビジュアル・グラフビルダ（LangGraph型, 大） |
| ツール選択(提案8) | §1のMCP正規化で「MPから取得→エージェントに紐付け」 | OpenAPI/SQLからMCPラッパー自動生成（中） |
| データソース選択(提案10) | RAGを Provider Interface 化、agent定義に`knowledge_base_id`を持たせる（中） | KB自体もMP公開リソース化 |
| 作ったものを公開(提案11) | Tier A配布に乗る（追加コスト小） | — |

---

## 5. 決定軸⑤: ノーコード / ローコード（提案14行目）

- **ノーコード基盤**: 宣言的マニフェスト（usecase/agent/kbをJSON）で大半カバー済み。
- **ローコード = GenAIで補完**:

| 方式 | 内容 | 工数/効果 |
|---|---|---|
| **Builder Copilot（採用候補）** | 「〜する業務アプリを作って」→ GenAIがusecase/agentマニフェストを生成 | 工数小・効果大（既存モデル流用） |
| ツール自動生成 | OpenAPI/SQLスキーマからMCPラッパー生成 | 中 |
| コードインタープリタ併用 | 軽い変換ロジックはOCI組込code_interpreterで実行 | 既存機能で対応可 |

---

## 6. 推奨ロードマップ（段階導入）

| Phase | 内容 | 規模 | 依存する決定 |
|---|---|---|---|
| **A** | Tier A（usecase/agent定義）の共有・公開。AgentBuilderにframework選択露出 | 小・即効 | レジストリ=§2-A（Object Storage+index） |
| **B** | MCP正規化でツールMP。No.1-RAG/SQL-AssistをMCP化して吸収。RAGをProvider Interface化 | 中 | §1-A, §3-② |
| **C** | Builder Copilot（GenAIマニフェスト生成）でローコード体験 | 中 | §5 |
| **D** | 専用レジストリμService（§2-B: 署名・バージョン・評価）、伝ぴょん等の外部連携、フロント動的ルート | 大 | §1-A/C, §2-B/C, §3-① |

- 「アプリ集 → 開発プラットフォーム」(提案13行目)への転換は **Phase A+Bで実質達成**。残りは規模と信頼性の話。

## 7. 採用結論（暫定）

- **隔離モデル**: 実行コード伴うプラグイン = **MCPサーバ正規化**（§1-A）。公式のみIn-process許容。
- **レジストリ**: PoC = **Object Storage+index**（§2-A）→ 本番 = **専用μService**（§2-B）、コンテナはOCIR併用。
- **既存資産**: RAG/SQL-Assist=**MCPツール化**、伝ぴょん=**外部アプリ連携**（§3）。
- 意思決定が要る軸（§1 隔離モデル / §2 レジストリ方式）は**ADR化して人間レビュー**に回す。

## 8. 残課題 / 次アクション

- [ ] §1・§2 を ADR草案化（`docs/decisions/`）して人間承認
- [ ] マニフェスト・スキーマの確定（type/runtime/permissions/version/署名）
- [ ] PoC: Phase A（usecase公開 + Object Storageレジストリ）の実機検証 → `docs/verification/`
- [ ] No.1-RAG のMCPツール化スパイク（検索パイプラインのHTTP/MCP境界切り出し可否）
- [ ] プラグイン署名・検証フローの定量的なセキュリティレビュー方針

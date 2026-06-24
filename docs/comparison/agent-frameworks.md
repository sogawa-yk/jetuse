# 比較: OCI Enterprise AIでのエージェント実装方式（FW-04）

フルスクラッチ実装と主要エージェントフレームワークを**すべてOCI実機（大阪・gpt-oss-120b）で検証**した比較。
エビデンス: SPIKE-12/13/14（spikes/）、docs/verification/FW-01.md / FW-02.md / AGT-01〜04。

## 結論（推奨の早見）

| 顧客・案件の類型 | 推奨 |
|---|---|
| 標準的なツール使用エージェント（業界標準スタックを希望） | **OpenAI Agents SDK**（本アプリの既定 — ADR-0008） |
| 複雑な業務フロー（分岐・並列・状態機械）の自動化 | **LangGraph** |
| 役割分担型のマルチエージェント（調査→執筆等のクルー） | **CrewAI** |
| Next.js/TypeScriptフロント直結・UIストリーミング重視 | **AI SDK（Vercel）** |
| Responses固有機能が必須（code_interpreter / server-side MCP / OCI Conversations） | **フルスクラッチ（Responses直）** |

## 機能マトリクス（OCI実機検証結果）

| | フルスクラッチ(native) | OpenAI Agents SDK | LangGraph | CrewAI | LangChain(LCEL) | AI SDK(TS) |
|---|---|---|---|---|---|---|
| 検証状態 | 本番稼働(AGT-01〜) | 本番稼働(FW-01) | 本番稼働(FW-02) | スパイク(SPIKE-14) | スパイク(SPIKE-14) | スパイク(SPIKE-14b) |
| API系統 | **Responses** | chat completions(※1) | chat completions | chat completions | chat completions | chat completions |
| function tool | ✓ | ✓ | ✓ | ✓(Crew内) | ✓ | ✓ |
| streaming | ✓ | ✓ | ✓ | — (未検証) | ✓ | ✓ |
| マルチエージェント | —(自前実装次第) | ✓ handoffs | ✓ グラフ(並列/分岐) | ✓ クルー(役割分担) | — | —(自前) |
| 承認フロー(HITL) | ✓(自前) | ✓ needs_approval+RunState直列化 | △ checkpointer前提(ステートレス往復に不向き) | △ human_input(対話前提) | — | △(自前) |
| guardrails | —(自前) | ✓ | —(自前ノード) | △ | — | — |
| MCP | ✓ server-side(type:"mcp") | ✓ client-side | △ アダプタ別途 | △ | △ アダプタ | ✓ (experimental) |
| code_interpreter / file_search built-in | ✓ | ✗(※2) | ✗ | ✗ | ✗ | ✗ |
| OCI Conversations(短期メモリ) | ✓(チャットで使用) | ✗ | ✗ | ✗ | ✗ | ✗ |
| 実装規模(本アプリ統合分) | 約220行+α | 約330行 | 約180行 | — | — | — |

※1 SDK既定のResponses直結はOCIの厳格input検証と不整合で**不可**（ADR-0007。input4パターン対照実験）
※2 file_searchは **DPの `vector_stores.search` をfunction tool化**することで代替可能（FW-01bで実装・本番稼働）

## 認証（IAM署名）の注入方法 — 各FWで実機確定した正解

OCIのOpenAI互換APIはAPIキーではなく**IAMリクエスト署名**のため、各FWのHTTP層への注入方法が導入の鍵。

| FW | 注入方法 |
|---|---|
| openai SDK / Agents SDK | `OpenAI(http_client=httpx.Client(auth=oci-genai-auth))` |
| LangChain / LangGraph | `ChatOpenAI(http_client=..., http_async_client=...)`（同上のhttpx） |
| CrewAI 1.14+ | `LLM(interceptor=BaseInterceptor実装)` で `on_outbound` 署名。**`provider="openai"` 明示必須**（モデル名が既知リストに無いとlitellmフォールバックでエラー） |
| AI SDK (TS) | `createOpenAI({fetch: customFetch})` で `oci-common` の `DefaultRequestSigner` により署名 |

共通ヘッダ: `CompartmentId` + `OpenAi-Project`（Enterprise AIプロジェクトOCID。無いと誤誘導エラー — ADR-0007）

## 各方式の所感（検証時の実測に基づく）

- **フルスクラッチ**: Responses固有機能（OCIサンドボックスのcode_interpreter、server-side MCP、会話状態）を
  全部使える唯一の選択肢。承認・再試行・上限処理を自前で書く分のコード量と保守責任が残る
- **Agents SDK**: handoffs/guardrails/HITLがSDK機能として揃い、**RunState直列化でステートレスHTTPの
  承認往復が状態DBなしに組める**のが効いた。崩れたfunction引数（gpt-oss実機）への寛容化は必要
- **LangGraph**: 並列・分岐のグラフが素直に書ける（2観点並列→統合が実測4.0秒）。ReActループは
  答えが見つからないと同一ツールを延々再試行する傾向 → 再帰上限の優雅な終了が実用上必須
- **CrewAI**: 役割分担クルーは少コードで動くが、HTTP層注入（interceptor）とsqlite3≥3.35要件
  （OL9はpysqlite3-binaryで差し替え）の足回り整備が必要
- **AI SDK**: TSフロントから直接OCIを叩ける（BFF不要構成も可能）。署名鍵をブラウザに置けないため
  実運用ではサーバー側（Next.js Route Handler等）に置く

## 制約の共通根（プリセールスで説明する際の要点)

フレームワーク経由は現状すべて**chat completions系統**になる（Responses直結はOCIの厳格スキーマと
各FWの簡易input形式が不整合 — ADR-0007）。よって「Responses固有機能を使うか」が最初の分岐点。
OCI側がスキーマを緩和（または各FWが厳格形式を送出）すれば、この制約は解消されうる。

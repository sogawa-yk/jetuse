# 比較: フロント生成（OpenCode + OCI モデル）のランタイム方式（SP3 / specs/19 §4）

日付: 2026-07-07（実測追記: 2026-07-08）。対象: specs/19-sp3-builder.md §4.4 の委譲論点の予備整理。
**本ドキュメントは決定しない** — 決定は SP3-03 の ADR（OpenCode 統合方式・adr_approval 人間ゲート →
ADR-0023 ドラフト起草済み）。定量欄は下記「実測」に反映済み（詳細 docs/verification/SP3-03.md）。

## 実測（2026-07-08 / SP3-03 第一関門。環境: dev インスタンス + 大阪 GenAI + jetuse:dev 実 CI）

| 項目 | 実測値 | 含意 |
|---|---|---|
| B1 署名プロキシ経由 chat.completions | HTTP 200 / **0.3 秒**（gpt-oss-120b, 非stream）。SSE 素通し成立 | **B1 成立**（技術リスクの核が解けた）。プロキシは Starlette 約50行（`spikes/sp3_03_sign_proxy.py`） |
| OpenCode headless 生成（プラン→SPA→build 緑） | gpt-oss-120b: **24 秒**（ホスト直接）/ **中央値 96 秒**（提案シェイプ相当 1cpu/4GB・ロックダウン ×3 = 95/165/96s）/ 3 分 26 秒（2cpu/2GB・初回） | N1 目標 p50≤5 分に対し内側。DNS 遮断でも完走（遅延は増える） |
| 生成成功率（この検証内） | gpt-oss-120b: **2/2 成功**・静的検査（生 fetch/絶対 URL）違反ゼロ。**gemini-2.5-flash: 0/1**（15 分 timeout・成果ゼロ — 疎通可でも agentic 完走せず）。**cohere.command-a 系は不成立**（chat.completions が 400 Unsupported OpenAI operation） | 既定モデル = gpt-oss-120b。F2 のモデル切替は provider 宣言+設定で成立（allowlist 既定は実証済みモデルに絞る — ADR-0023） |
| OpenCode ピーク RSS | **614MB**（生成+build、ホスト実測） | N7: メモリ 4GB シェイプで十分 |
| コンテナ起動オーバーヘッド（podman, warm） | **約 0.7 秒** | A/B 差はほぼ CI 起動時間のみ |
| **Container Instance 起動レイテンシ**（実 CI, node:22-slim, 1ocpu/4GB） | **43 秒**（CREATING→ACTIVE。jetuse-spike- 使い捨てで実測） | B の実体を OKE Job でなく CI にしても N1 に対して誤差圏 |
| オフラインビルド（vendored node_modules 41MB) | `--network=none` で **1.2 秒**（起動込み）成功 | **C1 成立**。npm レジストリ egress 不要 |
| 前提の実態 | **jetuse:dev に OKE クラスタは存在しない**（2026-07-05 リセットで破棄）。現行デプロイ実体 = RM スタック `jetuse-dev-app` の Container Instance | 軸A の「A2=OKE Job」は現環境では成立せず、**B の実体 = 生成ごとの Container Instance** に読み替え |

前提（specs/19 §4 の要件枠）: 生成ごとに使い捨てのサンドボックス（S1）・資格情報ゼロ（S2）・
egress は LLM エンドポイントのみが理想（§4.4-3）・CPU/メモリ/ディスク上限（N7）・
ハードタイムアウト 15 分（N1）。実行環境は OKE（本体デプロイ済み — ADR-0017 系の配備）
またはプレビュー用 Container Instance。

## 軸A: OpenCode をどこで回すか

| 観点 | A1: API コンテナ内 subprocess | A2: 生成ごとの専用コンテナ（OKE Job）**← 推奨仮説** | A3: 常駐ワーカーコンテナ |
|---|---|---|---|
| 隔離（S1） | **弱い**。同一コンテナ内のプロセス分離のみ。作業 dir の閉じ込め・API プロセスのメモリ/資格情報（DB ウォレット・RP トークン）との同居が原理的に残る。seccomp/rootless 追加でも「JetUse 本体に到達できない」を構造では言えない | **強い**。コンテナ境界 = FS/プロセス/資格情報の分離が構造的。NetworkPolicy で egress を Pod 単位に絞れる | 中。ワーカー自体は分離できるが**生成間の使い捨てが弱い**（前の生成の残骸・プロンプトインジェクションの持ち越し）。ジョブごと再作成なら実質 A2 |
| リソース上限（N7） | cgroup は API コンテナと共有 — 生成の暴走が API 本体を巻き込む | Pod の requests/limits で生成単位に確定 | ワーカー単位では可、生成単位では追加制御が必要 |
| 起動レイテンシ | ゼロ | Job スケジュール + イメージ pull（ノードにキャッシュされていれば秒〜十秒オーダー）。**N1 が分オーダーのため誤差** | ゼロ（常駐） |
| 追加部品 | なし（subprocess + timeout） | Job 起動・完了監視・ログ回収（kubectl/API 経由）。生成イメージ（OpenCode + Node + vendored scaffold）の配布（OCIR） | ジョブキュー or ポーリング + ワーカーのライフサイクル管理（現行構成に無い常駐部品） |
| 失敗の封じ込め | プロセス kill。ゾンビ/一時 dir の掃除は自前 | Pod 削除で完全に消える（ttlSecondsAfterFinished） | ワーカー再起動運用 |
| コスト | 追加ゼロ | 生成中のみ Pod 1 つ（SA 個人規模で常時コストなし） | 常駐分のリソースを遊ばせる |
| プレビュー環境（Container Instance 単体）との整合 | 動く | **要検討** — CI 環境に K8s Job は無い。プレビューでは A1 相当へのフォールバック or OKE 側でのみ生成を有効化 | 同左 |

**推奨仮説 = A2（OKE Job）**。S1/S2/N7 がコンテナ境界で構造的に満たせることが決め手。
確認すべき実事実（ADR 前の実機検証項目）: (1) API Pod に Job 作成の最小 RBAC を付与する運用
（IAM でなく K8s RBAC — 人間ゲート要否の整理）、(2) イメージサイズと pull 時間、(3) プレビュー
環境（OKE 非経由）での縮退方針。A1 を選ぶ場合は「rootless + 専用 UID + ネットワーク遮断 +
資格情報の非マウント」で S1/S2 をどこまで詰められるかを ADR で立証すること。

## 軸B: LLM 認証の経路（S2: 生成プロセスに署名鍵を持たせない）

| 観点 | B1: 署名プロキシ（sidecar / 内部サービス）**← 推奨仮説** | B2: OpenCode へ資格情報を直接渡す |
|---|---|---|
| 仕組み | JetUse 側に OpenAI 互換のリバースプロキシを置き、IAM 署名（リソースプリンシパル）を注入。OpenCode の provider 設定は `base_url=プロキシ` + ダミー key | OpenCode のプロセス環境に OCI 資格情報を渡し、独自に署名させる |
| 実現性 | oci-genai-auth は openai-python 専用だが、**署名部だけを薄い ASGI プロキシに載せ替え可能**（既存の genai.py 署名実装を流用） | OpenCode は OpenAI 互換 API key 認証のみ想定 — **IAM 署名のプラグイン点が無い**（要実機確認。無ければ成立しない） |
| S2 適合 | **適合**。サンドボックスは鍵レスで、プロキシ側でレート・モデル・監査（N5 usage_log）も一元化できる | 不適合（サンドボックス内に鍵）。プロンプトインジェクションで生成エージェントが鍵を出力しうる |
| 追加部品 | プロキシ 1 つ（薄い。egress 許可先もプロキシ 1 点に絞れる — §4.4-3 と好相性） | なし |

**推奨仮説 = B1**。B2 は S2 違反のため、B1 が実機で成立しない場合は方式ごと ADR で再検討。

## 軸C: ビルドの egress（npm レジストリ問題）

| 観点 | C1: vendored scaffold（依存焼き込み・オフラインビルド）**← 推奨仮説** | C2: npm レジストリへの egress 許可 |
|---|---|---|
| egress | LLM（プロキシ）のみ — 最小 | + registry.npmjs.org 等（ミラー運用しない限り外部到達を開ける） |
| 供給網リスク | 生成時のインストールなし = 生成時の supply chain 変動なし（イメージビルド時に固定） | 生成のたびに解決 — バージョン浮動・改ざん面 |
| 制約 | 生成コードはスキャフォールド同梱の依存しか使えない（プロンプトで明示）。**specs/19 §4.3 S3(a) の固定クライアント方針と整合** | 自由だが S3 の静的検査対象が増える |

**推奨仮説 = C1**。スキャフォールドに React + ビルドツール + 固定 API クライアントを同梱し、
OpenCode には「同梱依存のみでの実装」を課す。

## 軸D: 非同期の実行体（API 契約は specs/19 §4.5 で不変: 202 + status ポーリング）

| 観点 | D1: FastAPI BackgroundTasks | D2: OKE Job の完了を API が監視 |
|---|---|---|
| 追加部品 | ゼロ | Job watch（ポーリングで足りる） |
| 生存性 | API プロセス再起動で生成が消える（→ failed 遷移の検知が必要 = provisioning のまま孤立する行の reconcile） | Job は API と独立に完走。API 再起動後も status 確認で追従可 |
| 整合 | A1 と対 | A2 と対（軸A の選択に従属） |

孤立 `provisioning` の扱い（どちらでも必要）: タイムアウト（N1）を超えて `provisioning` のままの
demo は起動時/定期 reconcile で `failed` へ落とす（specs/18 の「pending 期限切れ回収」と同じ流儀）。

## まとめ（ADR への引き継ぎ）

推奨仮説の組: **A2 + B1 + C1 + D2**（フォールバック候補: プレビュー環境では A1 縮退の可否を検証）。
SP3-03 は着手時にまず (1) OpenCode headless の実挙動（大阪モデルで動くか・生成品質）、
(2) B1 プロキシの成立、(3) A2 の RBAC/イメージ運用 — を実機で確かめ、実測値とともに ADR を
起草して人間承認を得る。**技術限界に当たったら実装を止めて findings を残す**
（STAGE3-PROGRESS の施主方針 2026-07-07）。

**実測後の帰結（2026-07-08 → ADR-0023 ドラフト）**: OKE 不在の実態により A2 は
**B'（生成ごとの Container Instance）**へ読み替え。B1・C1 は実機成立を確認。実行体は
D1（BackgroundTasks がオーケストレーション）+ 孤立 provisioning の reconcile（どの方式でも必要）。
採用案・成果物受け渡し（署名プロキシへの POST — 書き込み PAR は棄却）は ADR-0023 参照。
**N1/N7 は暫定値**（N1 は実 CI 分布未測定の推定・N7 の PID/ディスク上限は未検証で承認ゲートに残る —
確定でない。限定表現と未検証範囲は ADR-0023 §決定 5 参照）。

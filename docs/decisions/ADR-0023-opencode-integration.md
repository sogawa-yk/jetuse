# ADR-0023: OpenCode 統合方式（フロント生成ランタイム・LLM 認証経路・egress・実行体・上限）

- Status: **Accepted（人間承認済み 2026-07-08 — adr_approval クリア。実装フェーズへ）**
- Date: 2026-07-08

## 人間決定記録（2026-07-08・施主承認）

承認ゲートの 3 未解決点について、以下の決定で **ADR を Accepted** とする（**Internal 限定前提・PoC-first =
まず動くもの（コンセプト確認）を優先**）:

1. **生成 CI のネットワーク隔離（open-item #1）= 完全隔離は要求しない**。Internal ユーザー限定前提でリスク許容。
   専用コールバック面の NSG/ingress 実 CI 検証・別立てジョブ専用ブローカーは**不要**、シンプルな到達構成でよい。
2. **自由 JSX 生成を継続で承認（open-item #2）**。同一オリジン配信によるデモ間 API 横断はリスクとして許容
   （デモ別オリジン・opaque-origin sandbox は**作らない**）。ただし**既存の安価な層（固定 API クライアント・
   バンドル静的検査・配信時 CSP）は仕様どおり維持**する。
3. **N7（プロセス数/ディスク上限）の強制方式（open-item #3）= 未検証のまま許容**。CPU/メモリ上限 +
   15 分ハードキル + N2 バンドル検査で足りる。
- **将来方向**: これらの問題が顕在化した際は **Container Instances → OKE 移行**、および**監査（audit）の仕組みの
  充実**で対応する（下記「リスクと残課題」に将来方向として記録）。
- **実装への影響**: 緩和で不要になった受け入れ条件（NSG 負の E2E・別オリジン隔離・PID/ディスク強制の境界 E2E）は
  **承認済み緩和**として実施しない（`SKIPPED.md` に理由明記）。**モデル切替可能（F2）は実装必須**。
- Context: SP3-03 / specs/19-sp3-builder.md §4.4（委譲 6 項目）・§4.1〜§4.3（要件枠）/
  予備比較 docs/comparison/frontend-generation-runtime.md / 実測 docs/verification/SP3-03.md
  （証跡 `runs/2026-07-08T0047_SP3-03/e2e/`）

## PoC 受理と descope 記録（2026-07-08・施主判断 / SP3-03 実装フェーズ完了時）

施主 PoC-first 決定（上記）の**適用**として、SP3-03 実装フェーズは「host E2E でプラン→生成→配信→
chat/rag 実応答→DELETE prefix ゼロ」を**達成・再検証済み**（Codex 実ブラウザ検証 PASS・grounded RAG＝
Chicago fallback・710 unit 緑）をもって **PoC 達成として受理**する。以下は本フェーズの実装対象から外し、
**承認済み後続タスク**へ割り当てる（ADR 自身の分担と一致。停止規律により本ループでは追わない）:

- **生成 runtime の実 Container Instance 化（§1 決定 B'）= デプロイ／将来**。本フェーズは S1（鍵レス・
  network=none・RO node_modules・クリーン scaffold + 検証済み src のみ）と N7（cpus/memory/pids・
  15 分ハードキル）を**満たす podman 近似**で実装・実証する（ADR 本文が「本フェーズ = podman 近似」と明記）。
  実 CI 化（OCIR pull・IAM・2 相 CI ライフサイクル・配備像同梱）は Container Instance 化タスク（→将来 OKE）。
- **一回性コードの ADB 単回失効（§2/§3.5「1回で失効」）= SP3-05**。本フェーズはサーバ側契約 + 単体契約
  テスト（Cookie なし=401／有効 Cookie=200／期限切れコード=401／owner mutation=403）を担保する（§3.5 が
  SP3-03 に課す範囲）。ステートレス HMAC の**単回失効（ADB ジョブ記録）と AUTH=true 実トークン全経路 E2E**は
  SP3-05（この経路は**実 PoC の AUTH-off プレビューでは未使用**）。
- **本番堅牢化 major 群 = 後続**: 起動時/定期 reconcile・PATCH と公開の直列化・N4 の bundle 保存/1MB 切詰/
  失敗試行保持・N5 実トークン帰属・配信 Cache-Control 失効整合・Lease 基盤障害の 503・app-session の
  X-JetUse-App 検証・公開失敗時の未公開 prefix 掃除。file:line 付き一覧は STATE.md residual と review-17.json。

> 位置づけ: これは ADR の技術決定の**変更ではなく**、施主 PoC-first 判断の下での**実装フェーズの範囲確定
> （descope）**。上記項目は「不要」ではなく「後続タスクで実施」。Codex はこれら descope 済み項目を根拠に
> FAIL を出しうるが、判定は書き換えず、施主がステージ報告で人間ゲートとしてトリアージする。

## 追記（2026-07-09 / SP3-08）: §1 決定 B' の実 Container Instance 化完了 — descope 項目①の解消

descope 記録の第 1 項「生成 runtime の実 Container Instance 化 = デプロイ/将来」を **SP3-08 で実装・
実機実証済み**とする（証跡 `runs/2026-07-09T0249_SP3-08/e2e/`・詳細 docs/verification/SP3-08.md）。
実装形（§1 の決定に対する具体化。差分があった点のみ記す）:

- **バックエンド切替**: `Settings.generation_runtime`（podman | oci-ci）。ローカル開発・単体テストは
  podman/モックのまま、jetuse:dev 配備は dev-app tf が `oci-ci` を配線。検証・deadline・再試行の枠は
  両バックエンド共通（`build_frontend` が単一シーム）。
- **成果物受け渡し = ジョブ専用 Object Storage prefix + 期限付きオブジェクト単位 PAR**
  （`jetuse-builder-jobs/<job_id>/`。読取専用/書込専用を相・方向ごとに分離 — 実機の負の確認で
  読取 PAR の PUT・書込 PAR の GET とも 404 拒否）。本 ADR §3 は「書き込み PAR 配布案は不採用」と
  したが、これは**配信面**（利用者向け URL）の判断。CI への相スコープ・短命（相タイムアウト+5 分）・
  オブジェクト単位の受け渡しは、ジョブトークン基盤（§2 フル実装 = 後続）なしで S2（資格情報ゼロ）を
  満たす PoC 経路として採用（タスク指示の推奨方式。漏洩残余 = 当該オブジェクト 1 個・期限内のみ）。
  **PAR はリソースとしても後始末する**（期限切れはアクセス無効化のみ — 発行 id を保持してジョブ
  finally で削除 + reconcile が期限切れ `jetuse-builder-` PAR を掃除。codex review-2 M001）。
- **CI 削除は終端確認まで**（codex review-2 M003）: 各相の削除は有界再試行 + DELETED 到達を
  ポーリング確認してから次相へ進む（「同時には 1 つ稼働」を要求でなく状態で保証）。確認不能時は
  reconcile が回収。reconcile の provisioning→failed 遷移は **demo リース下**で行い、_publish の
  ポインタ切替〜ready 遷移と直列化（codex review-2 M002。閾値も N1+データ投入余裕 = timeout+10 分）。
- **S2 実機確認（負の E2E・受け入れ条件充足）**: 生成 CI は `is_resource_principal_disabled=true`・
  env に資格情報/OCID なし。実 CI 内から RP 環境変数の不在・メタデータ/RPST fetch の ECONNREFUSED・
  未認証 OCI API 401 を確認。多層防御 (ii)（DG matching rule からの除外）は IAM 人間ゲートのまま。
- **イメージ**: OCIR public repo 2 本（jetuse-dev-gen / jetuse-dev-build — ADR-0011 の匿名 pull で
  CI に pull secret を配らない）。生成イメージに `@ai-sdk/openai@4.0.9` を**事前導入**
  （SP3-06 residual 解消 — `--network=none` で provider ロード成立を確認）。信頼ビルドイメージは
  OpenCode 非搭載 + `npm ci` 焼き込み（実行時 npm 解決なし = §3 C1 のまま）。
- **N4 ログの実装差分（実機 findings）**: Container Instance の `retrieve-logs` は**コンテナ INACTIVE 後
  409 で取得不能**（実測）。よって一次経路は「相スクリプト自身が trap EXIT で自ログを書込専用 PAR へ
  PUT」する方式（失敗時も opencode ログが `config.generation.error` に残ることを失敗系 E2E で実証）。
  `retrieve-logs` は起動失敗（スクリプト未走行）時のフォールバック。OCI Logging フォールバックは
  descope のまま（ハードキル時のログ欠落は残余）。
- **reconcile（§4）**: 起動時 + 5 分周期で (a) `jetuse-builder-*` 命名かつ N1+3 分超過の孤児 CI 削除、
  (b) N1 超過 provisioning demo の failed 化、(c) 残置ジョブオブジェクトの掃除。孤児 CI の実機回収を確認。
- **プロキシ到達（SP3-07 residual 解消）**: 生成 CI → API private IP `:8000/gen-proxy/v1` の VCN 内到達を
  実機確認。`GENERATION_PROXY_URL` が localhost（API 内 mount 自己参照）のとき runtime が自 IP へ解決。

## 決定（案）

specs/19 §4.4 の 6 項目を次のとおり確定する。

### 1. ランタイム = **B'（生成ごとの使い捨て Container Instance）+ 生成と検査/ビルドの分離（2 相）**

- **生成ジョブ 1 件 = 順次 2 つの使い捨て Container Instance**（同時には 1 つ稼働。詳細トポロジーは
  下記「2 相のトポロジー」）。各 CI は CI.Standard.E4.Flex（1 OCPU / 4GB）で、相ごとのタイムアウトで削除。
  相1 = OCIR の**生成イメージ**（node:22-slim + OpenCode + vendored スキャフォールド）、
  相2 = OCIR の**信頼ビルドイメージ**（node:22-slim + vendored scaffold・OpenCode 非搭載）。
- 予備比較の推奨仮説 A2 は「OKE Job」だったが、**実態として jetuse:dev に OKE クラスタは存在しない**
  （2026-07-05 のリセットで破棄済み — 実測で確認）。現行のデプロイ実体（RM スタック `jetuse-dev-app`）と
  同じ Container Instance を生成単位で使い捨てるのが、追加運用部品なしで S1/N7 をコンテナ境界で
  満たす最短経路。API（CI 上のリソースプリンシパル、`manage all-resources in compartment jetuse:dev`
  — 既存 DG jetuse-internal-dg）から CI の create/delete が可能。

- **サンドボックス自身に検査を任せない（codex-review F001）— 生成相と検査/ビルド相を分離する**:
  生成コンテナは `bash: allow` で node_modules・検査器・信頼原本まで書き換えられるため、その中で
  走らせた静的検査・ビルドは信頼できない。よって:
  1. **生成相（信頼しない）**: 生成コンテナは **`src/`（生成対象ソースのみ）を書ける領域**に限定して
     OpenCode を回す。信頼原本（`client.js`・`package*.json`・`vite.config.js`・`index.html`・
     node_modules・スキャフォールド）は**別マウントの読み取り専用**（overlay の lower や
     read-only bind）。成果物は **dist ではなく「生成ソース差分（src/ 配下）」**を API へ返す。
  2. **検査/ビルド相（ツールチェーンだけを信頼・入力は敵性として扱う — codex-review F006）**: API 側が
     **クリーンなコンテナを信頼済みビルドイメージから起動**し、成果物 API から生成 src 差分を取得して
     **`src/` 配下にのみ**（パス正規化・symlink/`..`/絶対パス/重複拒否のうえ）重ね、そこで
     **静的検査（層0〜2）→ `npm run build`**（dist を生成）→ dist を成果物 API へ POST する。信頼するのは
     **ツールチェーン（検査器・lockfile 固定の node_modules・vite）だけ**で、処理する生成ソースは敵性入力と
     みなすため、この相にも **S1 隔離を等しく適用**する: 非 root・`is_resource_principal_disabled=true`・
     API/DB 資格情報なし・read-only root + 限定 scratch（dist/`.vite` は tmpfs — trusted-build.log で実証）・
     CPU/メモリ/ディスク/PID 上限・LLM/OpenCode 非搭載。**egress は相1 と同じく「job-scoped 成果物/プロキシ
     API の 1 点のみ」**（LLM は使わないので実際に叩くのは成果物 API だけ。OCIR/Object Storage/他 OCI
     サービス/外部直 IP は不可）。**`npm ci` は実行しない**（node_modules は信頼イメージ焼き込みの
     **読み取り専用**を使う。再解決不能・不要 — codex-review F005）。dist はここで初めて生成され、検査合格した
     ものだけが staging → ポインタ切替（S5）に載る。この相にも負の E2E（RP/資格情報・成果物 API 以外の egress の
     不能）を受け入れ条件に含める。
  - **2 相のトポロジー（codex-review F005 — 単数 CI 前提の穴を塞ぐ）**: 1 生成ジョブ = **順次 2 つの
    使い捨て CI**（同時には 1 つ）。**ジョブ全体のハードタイムアウト = 15 分（N1）を相・起動・転送・
    削除の合算が超えないよう配分する**（codex-review F005）: **相1 = 生成 CI**（`jetuse-builder-gen-<job_id>`。
    OpenCode 実行・タイムアウト既定 **9 分**）→ plan を成果物 API から取得 → src 差分を成果物 API へ POST →
    相1 CI 削除。**相2 = ビルド/検査 CI**（`jetuse-builder-build-<job_id>`。信頼イメージ・LLM 非搭載・
    タイムアウト既定 **2 分**）→ src 差分を成果物 API から取得 → 検査 + build → dist を成果物 API へ POST →
    相2 CI 削除。相1(9) + 相2(2) + CI 起動 ×2（実測 ~86s）+ 転送/削除の余裕 ≈ 13〜14 分で 15 分内。
    ジョブ全体の経過が 15 分に達したら**どの相でも kill → failed**（各相タイムアウトの上に全体キャップ）。src/dist/plan の受け渡しは**すべて成果物 API 経由**（CI 間で直接やり取りしない。
    両相の egress は job-scoped 成果物/プロキシ API の 1 点のみ = §3 と一致）。BackgroundTasks が
    相1→相2 を駆動し、**全 CI を `jetuse-builder-*-<job_id>` の命名で識別**して、API 再起動・各相
    タイムアウト時の reconcile（孤児 CI の補償削除 + demo を failed）が job_id から exact に回収できる
    （§4 の reconcile が両相・両命名を対象にする）。API 再起動 + 各相タイムアウトのテストを受け入れ条件に。
  → S3(a)（固定クライアントだけが HTTP を出す）と S3(b)（静的検査）が、生成器の改変が届かない
     信頼境界で成立する。悪性 fixture（生成ソースが client.js/依存/検査器の改変を試みる）が
     公開に到達しないことを単体テスト（受け入れ条件）で担保。

- **S2（資格情報ゼロ）を構造で満たす — 2 段（codex-review F001/F003）**:
  - (i) **`is_resource_principal_disabled=true`** を生成コンテナ作成時に必須化する（OCI Container
    Instance のコンテナ単位設定）。これで RP 関連の環境変数・RPST 取得経路自体がコンテナに現れない。
  - (ii) 多層防御として、生成 CI を **既存 Runtime DG `jetuse-internal-dg` の matching rule 対象外**に
    配置する（生成専用サブコンパートメント、または rule を API CI に限定）。DG は同コンパートメントの
    computecontainerinstance 全体を対象にし得るため、(i) だけに頼らず二重化する。
  - **DG matching rule の現状確認・変更、コンパートメント配置は IAM 人間ゲート**（adr_approval と同時に
    人間が実施 — エージェントは IAM 読取権限なし。`oci iam dynamic-group list` = 404 で実測確認）。
  - **実装受け入れ条件（負の E2E）**: 実生成 CI 内から RP 環境変数の不在・RPST 取得の失敗・
    OCI API 呼び出しの失敗を実機確認して証跡化。
- 実測: CI 起動レイテンシ **43 秒**（CREATING→ACTIVE, jetuse-spike- で node:22-slim を実測・削除の
  終端状態 DELETED も証跡化）。2 相化で**CI 起動が 2 回加算**される点は N1 の見積り（§5）に織り込む。
  検査/ビルド相の build 自体は warm で 1 秒未満（gen4 実測）。
- ローカル開発・単体テストは LLM/OpenCode をモックした生成骨格のみ（specs/19 §9 SP3-03）。
  A1（API コンテナ内 subprocess）への縮退経路は**作らない**（二重実装の回避。S1 も満たせない）。

### 2. LLM 認証経路 = **B1（署名プロキシ）— 実機成立を確認済み**

- 「oci-genai-auth は openai-python 専用で OpenCode に注入不可」という技術リスクの核は、
  **薄い ASGI リバースプロキシに署名を載せ替える**ことで解決した（実証: `spikes/sp3_03_sign_proxy.py`
  約 50 行。`OciUserPrincipalAuth`（httpx.Auth）を `httpx.AsyncClient` に渡して素通しするだけ）。
- OpenCode 側は openai-compatible provider（`baseURL=プロキシ` + ダミー API key）。実測で
  chat.completions 0.3 秒・SSE 素通し・headless のツール呼び出しまで完走。
- 実装時: プロキシは API プロセスに内蔵する（署名は既存 `genai.py` の `_signer()` を流用 =
  RP/ユーザー署名の分岐も既存のまま）。**転送先は `POST /chat/completions` の完全一致 allowlist に
  限定する**（codex-review R9-F001 — 任意パス透過だと非信頼な生成相が Files/Responses/Conversations/
  Vector Stores 等を API の広い OCI 権限で叩けて「他の箱・OCI へ横移動不可」が崩れる）。クライアント供給の
  Authorization・OCI スコープ（Compartment 等）・署名関連ヘッダは**転送せずサーバ側で固定**し、
  method（POST）・Content-Type・本文サイズ上限・model allowlist を検証する。Files/Responses/
  Vector Stores/パストラバーサル/非 allowlist モデルが拒否される負の契約テストを受け入れ条件に含める
  （`spikes/sp3_03_sign_proxy.py` の純粋関数 `_reject` で実装・自己検査済み。実機で chat.completions=200・
  SSE 素通し成立、上記横移動=403、非 allowlist モデル=403、GET=405 を確認 —
  spike-v3-restricted-proxy.log）。生成 CI からのみ到達できるよう **ジョブスコープ・トークン**（Bearer）で
  認可する。**資格情報隔離の正本（唯一の方式）= 生成/ビルド CI は per-phase×operation のジョブトークンを保持し、
  専用コールバック面（署名プロキシ + 成果物/ログ API）を通常 API とは別の L3/L4 エンドポイント（private
  IP/LB/VNIC）に置き、生成 CI の egress をその 1 宛先のみに NSG 限定・コールバック面の ingress を生成 CI 送信元に
  限定する**（NSG は URL パスを識別できないため L3/L4 宛先で分ける。通常 `/api/*`・OCIR(pull 後)・メタデータ/
  RPST・外部直 IP を遮断 — §5.5(a)・open-item #1）。漏洩トークンの被害面は **ingress 制限（別送信元からの漏洩
  Bearer を拒否）+ job スコープ + 予算 + 短命 exp** で bound される（egress-lock 単独では漏洩トークンを絞れない。
  詳細・成立可否・full 隔離ブローカー option は §5.5(a)・open-item #1）。
- **トークンの束縛（codex-review F006/F004 — 自己完結クレームだけでは予算・一回性を強制できない）**:
  LLM は多数回呼ぶため推論トークンはジョブ生存中**再利用**する。予算・回数・一回性は署名クレームに
  書くだけでは並行リクエスト/複数 API プロセスでリプレイが通るため、**サーバ側のジョブ記録を
  単一真実源**とする。**ジョブ表は `job_id` を主キー**に予約 token budget・消費カウンタ・各操作の消費
  フラグを持ち、**操作トークン表は各操作の独立 `jti` を主キー**に相・操作・失効単位を個別管理する
  （codex-review R9-F007 — 両表の主キーを分離し、相×操作の一意性と個別失効を保つ）。これらを
  全 API プロセス共通ストア（= 既存の ADB。demo 排他リースと同じ DB）で
  **原子的に更新**する（残予算のデクリメントと upload 消費を `UPDATE ... WHERE remaining>0`／
  `WHERE upload_used=0` の原子条件で行い、0 行 = 予算切れ/二重 upload を 429/409）。
  トークンは **相 × 操作ごとに別トークン**を発行する（codex-review F001/F002/F004 — 共有 audience では
  相1 の資格情報で相2 の操作を呼べて信頼ビルド相を迂回できる）。各トークンは署名済みで
  **`job_id` + `phase`（1|2）+ `op`（許可操作 1 種）+ 独立 `jti` + `exp`**（相ごとのタイムアウト）を束ね、
  **各 CI にその相で必要な操作トークンだけを渡す**（相1 = {plan_read, genai,
  src_upload, log_append}、相2 = {src_read, dist_upload, log_append}）。`log_append` は相ごとに独立の再利用可能トークンで
  逐次ログ POST を認可する（codex-review R9-F004 — これが無いとログ POST が認証不能か、genai/upload
  トークンの流用で相×操作分離を崩す）。サーバ側ジョブ記録（job_id 行 + `jti` 消費表）が状態機械を原子強制する:
  - **`genai`**（相1のみ・再利用可）: allowlist モデルの推論。**予約→確定方式**で予算を守る。
    **予約量 = サーバが検証・clamp した (入力トークン上限 + 出力上限)** — プロキシは入力本文サイズ上限を
    強制（超過 413）し、その保守的換算 + **サーバが clamp した `max_tokens`** の合計を
    `UPDATE ... WHERE remaining >= :reserve` で原子予約（不足/並行超過は 0 行 = 429）、確定 usage で差額返却。
    **N5（コスト記録）**: プロキシは job_id → owner_sub（generate を叩いた実ユーザー）を保持し、確定 usage を
    **owner に紐づけて `usage_log` へ記録**する（specs/18 §3.2 手順4「監査は人に紐づける」と同じ）。
    ストリーム応答は最終チャンクの usage、切断・上流エラー・usage 欠落時は予約分を保守値として記録
    （記録漏れを作らない）。source ラベルは `builder:generate`。
  - **`plan_read`（相1）/ `src_read`（相2）**: 各々カウンタ上限。**`src_read` は `phase=2` トークンでしか
    呼べず**（相1 の資格情報では不可）、かつ `src_upload` 完了フラグが立って初めて許可（順序強制）。
  - **`src_upload`（相1）/ `dist_upload`（相2）**: 操作ごとの原子フラグで**一回性**強制
    （`UPDATE ... WHERE <flag>=0` の 0 行 = 二度目 409）。`dist_upload` は `phase=2` トークン限定
    （相1 のトークンでは dist を書けない = ビルド相の迂回を構造で塞ぐ）。サイズ上限を受領時に検査。
  - サーバが相を進める（相1 完了を確認して相2 CI を起動し `phase=2` トークンを発行）ため、相1 の漏洩
    トークンでは相2 操作を呼べない。状態機械の順序（plan_read → genai* → src_upload → [サーバが相2起動] →
    src_read → build → dist_upload）・terminal（failed/公開/exp）での全 jti 失効・各操作の並行リプレイ拒否を
    契約テスト（受け入れ条件）。漏洩時にできるのは当該相の未消費操作のみで、他デモ・箱・OCI へ横移動不可。
  **サンドボックスは OCI 署名鍵/RP を持たない**（S2 充足 — `is_resource_principal_disabled=true`）。
  job トークンは保持するが、egress の NSG-lock（専用コールバック面のみ）+ job スコープ + 予算 + redaction で
  漏洩残余を bound する（§5.5(a)。full 資格情報隔離が要るなら別コンテナブローカーを option として open-item #1 で判断）。
- **ログの redaction（F006 後段）**: N4 ログ書き込み前に**既知のトークン値そのものを完全置換**する
  （`Bearer <token>` 形式のマスクだけでなく、トークン文字列単体の出現も対象 — §5.5）。

### 3. egress / ビルドのオフライン化 = **C1（vendored scaffold）— 実機成立を確認済み**

- スキャフォールド（Vite + React + 固定 API クライアント + AGENTS.md — `spikes/sp3_03_scaffold/` を
  実装時に昇格）の依存を **package-lock.json で固定**（vite 6.4.3 / react 18.3.1 — lockfile 同梱済み）
  し、生成イメージ**ビルド時**（OCIR に push する前・ネットワーク可）に `npm ci` で node_modules を
  焼き込む（41MB）。**実行時（生成相・ビルド相）は node_modules を焼き込み済み・read-only で使い、
  `npm ci` も含め一切 npm 解決しない**。実測で `--network=none` ビルド成功（証跡 offline-build.log —
  コマンド・exit 0・生成物検査込み）・**DNS 遮断コンテナ内で OpenCode の生成〜build まで完走**。
  ベースイメージ・OpenCode バイナリも digest/バージョン固定（N6 の opencode_version 記録と対）。
- **実行時 egress の設計（codex-review F002 — OCIR pull は不可避。承認前に実 CI 検証が要る未解決点）**:
  Container Instance は起動時にコンテナイメージ自体を OCIR から pull する必要があり、**依存を焼き込んでも
  OCIR pull は消えない**（前案「実行時 pull ゼロ」は誤りだったため撤回）。したがって設計の要は
  「**pull は CI 起動フェーズ（インフラ層 = kubelet 相当）で完結し、その後起動する OpenCode/ビルドの
  コンテナ内プロセスからは OCIR も他 OCI サービスも叩けない**」を実 CI で成立させられるか、である:
  - 期待する成立根拠: (i) pull はコンテナプロセス外のインフラ層が行い、OCIR 資格情報はコンテナに
    渡らない、(ii) `is_resource_principal_disabled=true` + 資格情報ゼロで OCI サービスは未認証=叩けない、
    (iii) NSG/セキュリティリストで**コンテナからの egress 許可を署名プロキシ 1 点のみ**に絞る。
  - **これは本フェーズ（podman 近似・ADR 承認前）では検証できない技術限界**: 実 private OCIR イメージ +
    想定 NSG での実 CI 実験（pull 成功 → コンテナから OCIR/OS/PAR/他サービス/外部直 IP/メタデータ/RPST が
    いずれも失敗・プロキシのみ成功）が必要で、これはネットワーク/NSG 構成に踏み込むため**人間判断の
    範囲**（IAM/ネットワーク人間ゲート）。
  - **判断要求（task の「技術限界に当たったら判断を仰ぐ」）**: この egress 分離が実 CI で成立するかは
    **adr_approval の可否を左右する未解決点**として人間に上げる。成立しなければランタイム方式（B'）自体の
    再検討（例: 生成を OKE 復帰後の Job に載せ替え・NetworkPolicy で厳密化）が必要になりうる。
  - **正直な限定**: podman の DNS 遮断が実証したのは「名前解決なしでも生成が完走する」ことだけで、
    上記の実 egress 封じ込めは未実証。npm レジストリ・models.dev 等への実行時到達は不要と実証済み
    （遮断時は OpenCode の外部到達リトライで生成時間が延びる: 24 秒 → 3 分 26 秒。N1 内で許容）。
- 成果物の受け渡し（§1 の 2 相分離と一意に整合させる）: **生成相が返すのは正規化した src 差分のみ**
  （dist は返さない）。生成相はジョブトークンで**専用コールバック面の成果物エンドポイント**（§5.5(a) の
  NSG-lock された 1 宛先。通常 `/api/*` ではない）へ src 差分を POST する
  （サイズ上限・ファイル数上限・パス正規化で symlink/`..`/絶対パス/重複パスを拒否）。dist を作るのは
  **検査/ビルド相**（§1 の信頼コンテナ）であり、その dist だけが staging
  （`demo-bundles/<sha1(namespace)>/<bundle_id>/`）→ ポインタ切替（S5）に載る。ジョブ入力（プラン JSON・
  モデル設定）も起動時にプロキシから取得（CI の env に 256KB のプランを積まない）。
  → 台帳 write-ahead（S6）・静的検査 → ビルド → ポインタ切替（S5）は**すべて API 側 + 信頼ビルド相**で
  行い、生成相は公開成果物（dist）を作らない・書けない。specs/19 §5 の契約は不変。書き込み用 PAR 配布案は
  不採用（配信 PAR を不採用にした §5.2 と同様、URL 所持 = 権限の面を増やさない）。N2 20MB 上限は
  ビルド相が作った dist に対して API 側が検査する。

### 3.5 生成フロントの認証（AUTH_REQUIRED=true の配備での成立条件）

- デモスコープ能力ルートと `/app/` 配信は `require_demo`（→ `require_user` = Bearer JWT）に
  依存するため、**AUTH_REQUIRED=true では静的 SPA の読込・API 呼び出しとも素の GET では 401** になる。
- 決定: **トークンをバンドルへ焼き込まない**（S4 の秘密検査と矛盾するため構造的に禁止）。
  固定 API クライアントに**認証注入 seam**（`setAuth(bearer)` — 実行時にのみ供給）を設ける。
- **bootstrap の確定方式（codex-review F002/F007 — setAuth 単体でも postMessage 単体でも初回 HTML
  取得の 401 を解けない）**: `/app/{path}` の初回 HTML・相対アセット取得自体が `require_user`（Bearer）
  配下で、JS 実行前・相対 asset 要求へは Bearer も postMessage も継承されない。よって bootstrap は
  **一回性コード → HttpOnly Cookie 交換方式に一本化**する:
  1. 認証済みの親（ビルダー UI、Bearer 保持）が API の `POST /api/demos/{id}/app-session` を叩き、
     **一回性コード**を得る（require_user + 所有/公開判定 = require_demo と同一面）。
  2. 親は `/app/?c=<code>` を iframe/新タブで開く。配信ルートはコードを検証し、成功時に
     **HttpOnly・Secure・SameSite=Strict・`Path=/api/demos/{id}/`** の Cookie を Set-Cookie して HTML を
     返す。**一回性コードの寿命（数分）と Cookie セッションの寿命を分離する**（codex-review R10-F003 —
     Cookie を数分にすると正常に開いたデモでも数分後の chat/rag/dbchat が必ず 401 になり復旧経路が無い）:
     コードは短命（数分・1 回で失効）、**Cookie セッションは閲覧セッション相当（既定 60 分）**とする。
     **更新は親（Bearer 保持）による再発行だけに一本化する**（codex-review R11-F003 — Cookie 自身で呼べる
     refresh は生成 SPA が親なしで無限延長でき失効境界が崩れるため**不採用**）: 親が期限前に
     `POST .../app-session` で新コードを取得し `/app/` を再ロードして Cookie を巻き直す（iframe は親が駆動、
     新タブは親画面が生存する限り更新可能）。**親画面終了後は更新不可・期限で失効し復旧不可 = 仕様
     （再度ビルダーから開く）**。Cookie には初回発行からの**絶対有効期限**も併せ、延長の累積上限とする。
     受け入れ条件のテスト: 親終了後の更新拒否・絶対期限・期限境界・公開→非公開・別 demo Cookie 拒否。
  3. 以降の HTML・全 asset・能力 API 呼び出しは、**Bearer と Cookie を OR で受ける複合依存
     `require_app_or_user` で認可**する（codex-review R9-F003 — 現行能力ルータは共通で
     `require_ready_demo → require_demo → require_user` が掛かるため、Cookie 用依存を単に**追加**すると
     Bearer 不在の Cookie 経路は依然 401、逆に既存依存を**置換**すると Bearer 後方互換と毎要求の可視性/
     ready-deleting 再検証を失う）。`require_app_or_user` は:
     - **Bearer があれば既存 `require_ready_demo → require_demo → require_user` 経路を無変更で受理**する
       （Bearer クライアントは回帰なし。`require_user` 本体は変えない）。
     - **Bearer が無い場合のみ app-session Cookie を検証**し、かつ**毎要求 Demo を再取得**して
       可視性（公開→非公開で失効）・`subject`・ready/deleting・`exp` を **`require_ready_demo` と同一規則**で
       検査する（発行後に非公開化・削除開始されたデモへ期限まで到達させない）。Cookie は `demo_id`/`subject`/
       許可操作に束縛。
     - **`/app/` 配信と chat/rag/dbchat の明示 allowlist ルートにだけ適用**（app-session 再発行・owner
       mutation・DELETE には適用しない = 生成 SPA からは呼べない）。CSRF は SameSite=Strict + カスタム
       ヘッダ要求、Cookie は当該デモパスに限定。固定 API クライアントは Cookie 前提で **トークンを一切
       保持しない**（S4 と整合。`setAuth` seam は AUTH オフのプレビュー/将来の別方式用に残すが既定経路では
       未使用）。**受け入れ条件のテスト**: Bearer 回帰・公開→非公開・ready→deleting・期限切れ。
     - **⚠ 同一オリジンではデモ間隔離が成立しない（codex-review R13-F001 — open-item #2(b)）**: path 別 Cookie は
       デモ A の生成 JS がデモ B の URL を叩くと**ブラウザが B の path に一致する Cookie を自動送信**し、固定
       ヘッダ `X-JetUse-App` も生成 JS が付与できるため、A/B 双方を開いた利用者でデモ間横断が成立する。
       `require_app_or_user` の demo_id 束縛だけでは防げない（単純な「別 demo Cookie 拒否」テストでも検出不能）。
       真の隔離は**デモごとの別オリジン**または **opaque-origin sandbox** が要る = **承認ゲート open-item #2(b)**。
       負の E2E（A/B 双方の Cookie があるブラウザで A から B の能力 API 呼び出しが拒否される）を受け入れ条件に。
  - **新依存の追加（`require_app_or_user`・app-session 発行）は人間ゲート**（Public 面に影響しない
    Internal 限定。認証面の追加のため adr_approval と併せて確認。**`require_user` 本体は変えない**）。
    受け渡し UX（iframe か新タブか）は specs/19 §7 手順 4 どおり SP3-05 で確定。
  → **AUTH_REQUIRED=true での一連（コード発行 → HTML 取得 → 全 asset → chat）が通ること + owner mutation /
  app-session 再発行が生成 SPA から拒否されることの実トークン E2E は SP3-05 の受け入れ条件**。
  本フェーズ（SP3-03 実装）は AUTH オフのプレビュー環境（STAGE3-PROGRESS の E2E 方針）で配信・疎通を
  確認し、契約テスト（Cookie なし=401 / 有効 Cookie=200 / 期限切れコード=401 / app-session 経由の
  owner mutation=403）を単体で担保する。

### 4. 非同期の実行体 = **D1（FastAPI BackgroundTasks がオーケストレーション）+ reconcile**

- API 契約（202 + status ポーリング — specs/19 §4.5）は不変。BackgroundTasks が
  「CI 起動 → 完了ポーリング → 成果物受領 → 静的検査 → 公開 → CI 削除」を駆動する（追加部品ゼロ）。
- API 再起動でウォッチが消える件は、**孤立 `provisioning` の reconcile**（起動時 + 定期: N1 ハード
  タイムアウトを超えた provisioning を `failed` に落とし、残存 jetuse-builder- CI を削除）で吸収する。
  これはどの実行体でも必要な部品（比較ドキュメント 軸D）であり、D2 の優位は消える。
- **同時生成数 ≤2（N3）は本タスクで新規実装**（review-6 F008 / review-8 F004 — 「既存の 409 ガード」は
  存在せず、demo 排他リースは **demo_id ごとに別ロック**のため別デモの同時 generate を直列化しない）。
  実装は**全 API プロセス共通の単一グローバルロック**で行う: N3 専用の**固定名リソースロック**
  （`DBMS_LOCK` の単一固定ロック名 = 全 demo_id 横断で 1 本）を取得した区間で `provisioning` 件数を
  数え、`< 2` なら Demo を provisioning にして解放（超過は 409 detail「生成中のデモが多すぎる」）。
  **新規 INSERT と再生成（`failed → provisioning` の冪等上書き）の双方を同じ固定ロック下に入れ、
  件数検査 → 状態遷移 → commit までを 1 つの原子区間にする**（codex-review R12-F003 — 再生成が同じ
  ロック/件数検査を通らないと新規と再生成の競合で同時数が 2 を超える。commit 前にロックを解放すると
  後続から行が見えない）。デモ横断で直列化されるため件数チェックが原子になる。**競合テスト**（(a) 空状態から
  3 件同時 generate → 2 件 202・1 件 409、(b) 既存 1 件 provisioning から 2 件競合 → 1 件 202・1 件 409、
  (c) **新規 generate と `failed→provisioning` 再生成を混在させた 3 並行 → 2 件 202・1 件 409**）を
  受け入れ条件に含める（codex-review F008/R12-F003 — 単純な「2 件で片方 409」では上限 1 実装を誤って通す。
  新規/再生成混在の境界も検証）。CI の暴走は 15 分で API 側から削除（fail-closed）。

### 5. N1/N7 の値（暫定確定 — 根拠は 1cpu/4GB 相当の複数回実測。実 CI での分布計測を実装受け入れ条件とする）

| 項目 | 暫定確定値 | 実測根拠 |
|---|---|---|
| N1 生成時間 | **確定（2026-07-09 実 CI 計測 — SP3-08）: generate→ready 実測 101 / 101 / 141 / 202 / 241s（120b×3・gpt-5.6-sol・120b 再試行込み）= p50 ≈ 1.7〜3.4 分で目標 p50 ≤ 5 分内。ハードタイムアウト 15 分維持** | 実 CI 分布（jetuse:dev・E4.Flex 1ocpu/4GB・実生成/ビルドイメージ）: 相1（CI 作成→src 受領）51.7〜147.5s / 相2（CI 作成→dist 受領）31.1〜56.1s（CI 起動 ~40-60s と、review-2 M003 対応後は相間の DELETED 確認待ち ~+40s を含む）。証跡 runs/2026-07-09T0249_SP3-08/e2e/。旧推定（podman 近似 95/165/96s + 起動×2 ≈ 4 分）は実測で上書き |
| N7 CPU / メモリ | **1 OCPU / 4GB**（CI.Standard.E4.Flex — プレビュー API と同シェイプ） | 1cpu/4GB 制限で 3/3 完走（上記）。ピーク RSS 614MB（ホスト実測） |
| N7 プロセス数 | **未検証（実装受け入れ条件へ）**。上限案 = 512 | podman `--pids-limit 512` で成立性は測ったが、OCI Container Instance の resource_config は CPU/メモリのみで**プロセス数上限の強制方式が未定**（codex-review F006）。非 root ユーザーへの RLIMIT_NPROC を設定する監督プロセス等を実装フェーズで確定し、fork 過多で failed + CI 削除になる境界 E2E を受け入れ条件に |
| N7 ディスク | **未検証（実装受け入れ条件へ）**。上限案 = 書き込み領域 2GB（作業 dir + OpenCode 状態、超過は failed）。イメージ側は RO レイヤ約 610MB（node:22-slim 233MB + OpenCode 335MB + vendored deps 41MB） | 実測は**使用量が小さいこと**のみ（書き込み 62MB / OpenCode 状態 6.6MB / dist 152KB）。**2GB の fail-closed 強制方式（専用ボリューム quota / 監視 kill）と超過時挙動は未検証** |
| N2 バンドル | 20MB 上限（spec 既定を維持。実測 dist は 152KB） | vite build 実測 |

- **ディスク N7 は本フェーズ未確定（codex-review F005）**: 実測は使用量の小ささを示すだけで、
  2GB 上限の**強制方式と超過時の failed が裏づけられていない**。実装フェーズで実 CI の quota /
  専用ボリューム / 監視 kill のいずれかを確定し、**2GB 境界内の成功と超過時の failed + CI 削除を
  実測**することを受け入れ条件とする。それまで N7（ディスク）は「未検証」扱い。
  成果物受領時の N2 検査（20MB）は別途 API 側で fail-closed。

- **正直な限定**: 上記は podman による提案シェイプの近似であり、実 Container Instance（実生成
  イメージ・NSG 実条件）での測定ではない（実 CI では node イメージの起動 43 秒のみ実測）。
  **実装の受け入れ条件に「実 CI 上で生成ジョブを複数回実行し、CI 作成→成果物受領の分布から
  p50 ≤ 5 分を確認して証跡に残す」ことを含める**。分布が目標を外れた場合はシェイプ
  （OCPU 増）またはタイムアウトを本 ADR の追記で再調整する（15 分ハードキルは上限として不変）。

### 5.5 可観測性（N4）— 生成ログの保存先・保持・秘匿

specs/19 §4.2 N4 が ADR に委譲した保存先を次のとおり確定する:

- **bundle_id の割当てタイミング（codex-review F007）**: 生成試行の**開始時**（相1 起動前）に
  `bundle_id` を採番し、`config.frontend` は指さない staging prefix として確保する。ログ・成果物とも
  最初からこの `<prefix>/<bundle_id>/` 配下に書く（CI 起動失敗・生成失敗・検査失敗でも失敗ログが残る）。
- **保存先** = Object Storage の `demo-bundles/<sha1(namespace)>/<bundle_id>/logs/generation.log`
  （プロンプト・OpenCode 実行イベント・ビルド出力を 1 ファイルに連結。**bundle_id ディレクトリ配下**に
  置くことで、旧バンドル削除（§5.1 = `<prefix>/<bundle_id>/` 削除）でログも一緒に消え、デモ
  DELETE（§5.4 3g = `<prefix>/` 完走削除）でも回収される。独自の削除部品を持たない）。
- **失敗試行の保持・回収**: 未公開（`config.frontend` が指さない）bundle_id の staging は、
  **再生成時に前回の未公開 bundle_id を先に prefix 削除**してから新採番（未公開 prefix の無制限蓄積を防ぐ）、
  かつデモ DELETE の 3g が全 bundle_id（公開・未公開・失敗分）を prefix 完走削除で回収する。
  CI 起動失敗・生成失敗・検査失敗の各ログ保存と、再生成/DELETE での回収をテスト（受け入れ条件）。
- **保持期間** = 当該 bundle_id の生存期間（再生成の新採番で旧未公開 bundle_id を削除する際、
  その配下のログも同時に消える。デモ DELETE で全 bundle_id 分が 3g で消える）。
- **サイズ上限** = 全体 **LIMIT = 1MB（= 1,048,576 バイト）を超えない**（codex-review F008）。区切りは
  **固定長**にして N の桁数依存の循環を消す: `\n...[omitted]...\n`（固定バイト数 D）を用い、
  `head = tail = floor((LIMIT - D) / 2)` を各端へ配分（中間を削る。省略バイト数はログ末尾に別途 1 行で
  付す必要はない — 固定区切りで足りる）。切り詰めは UTF-8 コードポイント境界で行い端で多バイト文字を
  割らない（端数バイトは切り捨て → 最終バイト長は常に LIMIT 以下）。冒頭のプロンプト＋終盤のエラーの
  双方を残す。桁境界（9→10 等）と多バイト UTF-8 の組合せ境界テストを受け入れ条件に。
- **秘匿（redaction）**: ジョブトークン（Bearer）はログ書き込み前にマスクする。そもそも
  F1（秘密を入力に含めない）により OCID・資格情報はログに現れない構成が正で、redaction は
  多層防御。プロキシのアクセスログにも Authorization 値を残さない。
- **ログの取得経路（codex-review F006 — 成果物 POST だけでは kill/クラッシュ/通信断で失われる）**:
  Object Storage への書き込み主体は **API 側**（サンドボックスに Object Storage 権限を与えない — S2 不変）
  だが、CI 内ログを取りこぼさないため取得は**二系統**にする:
  - (1) **逐次ログ経路**: 生成/ビルド相は stdout を**job-scoped ログ API（成果物 API と同じ 1 点 egress）へ
    逐次 POST**（フェーズ境界・一定間隔でフラッシュ、単調増加の連番 chunk として）。**Object Storage への
    read-modify-write 追記はしない**（codex-review F007 — 複数プロセス/再送で断片消失・重複・切詰め不整合）。
    代わりに **chunk を `<prefix>/<bundle_id>/logs/<phase>/<seq>.part` の別オブジェクトとして put**し
    （**first-write-wins**: seq の初回書き込みのみ採用し、再送は**本文ハッシュ一致時だけ成功扱い**・
     不一致は 409 で拒否 — codex-review R11-F006。単純上書きは異本文の競合順で最終ログが非決定になる）、
    **generation.log は最終確定時にサーバが seq 順で連結して
    1MB 切詰め**（§5.5 のサイズ規則）を一度だけ適用する。→ 並行・再送・切詰めが全て決定的。
    **`log_append` は job_id/phase/seq/exp を検証し、chunk 単体サイズ・累積バイト・chunk 件数・受領レートに
    原子上限を課す**（codex-review R9-F004 — 上限が無いと非信頼プロセスが Object Storage を無制限に
    増やせる）。上限超過は当該 `log_append` を fail-closed（以降拒否）、terminal 後は `log_append` トークンを
    失効、`.part` は generation.log 確定後に削除する。並行送信・seq 再送・上限超過・terminal 後送信の
    契約テストを受け入れ条件に含める。
  - (2) **フォールバック = OCI Logging**: CI の stdout を OCI Logging に流す構成にし、hard kill・
    プロセスクラッシュ・起動失敗（相のコンテナが POST に到達しない）で (1) が欠けた場合は、
    reconcile が **CI 削除前**に OCI Logging から当該 job_id 分を回収して generation.log に統合する。
  - **(a) 資格情報隔離の正本（唯一の方式）= NSG-lock された専用コールバック面 + トークン漏洩の残余 bound**
    （codex-review R9-F002 / R11-F001 — 生成相は bash 可で継承 fd・`/dev/stdout`・`/proc/*/fd/*` から任意に
    ログ sink へ書けるため**同一コンテナ内 redaction は fail-closed にならない**。よって redaction は多層防御へ
    格下げし、一次制御をネットワーク隔離に置く）:
    - **専用コールバック面 = 独立した L3/L4 エンドポイント（一次制御・codex-review R13-F002）**: 署名プロキシ +
      成果物/ログ API を、**通常 API とは別の private IP / LB listener / VNIC** に置く（**NSG は URL パスを識別
      できない**ため、同一ホストで「`/api/*` は遮断・`/internal/genai` は許可」はできない = パスでなく L3/L4 宛先で
      分ける）。生成/ビルド CI の egress NSG は**この専用コールバック面の 1 宛先のみ**に限定し（通常 API・
      OCIR(pull 後)・メタデータ/RPST・外部直 IP を遮断）、**コールバック面の ingress は生成 CI の NSG 送信元
      からのみ許可**する。**同一ネットワーク名前空間の同居コンテナでは迂回を防げない（R11-F001）**ため隔離は
      L3/L4 宛先分離で行う。これにより生成プロセスは通常 API（`AUTH_REQUIRED=false` の無条件 dev-user 経路を
      含む）へ到達できず横移動不可。**この分離トポロジー（private LB/VNIC・ingress 制限）が実 CI で成立するかは
      open-item #1。成立しなければ別エンドポイントのジョブ専用ブローカーを option でなく必須にする（下記）**。
    - **トークン漏洩の残余 bound（正しい根拠は egress でなく ingress 制限 — codex-review R13-F002）**: CI は
      per-phase×operation トークンを保持する（成果物/ログ POST に必要）。仮に stdout/ログへ漏れても — (i)
      src_upload/dist_upload は単回消費・plan_read/src_read はカウンタ上限・順序/相強制でリプレイ拒否、(ii)
      再利用可能な genai は予算（予約→確定）+ job スコープ + 短命 exp に限られ、かつ**コールバック面の ingress を
      生成 CI 送信元に制限**すれば**外部のログ閲覧者は別送信元ゆえコールバック面へ到達できず漏洩 Bearer を
      使えない**（egress-lock は CI の外向きを絞るだけで漏洩トークンの被害面は絞らない — この bound の根拠は
      ingress 制限であり、その成立は open-item #1）、(iii) `log_append` は seq 冪等 + 累積/件数/レート上限。
      多層防御として token は既知プレフィックス（`jetuse-jt-`）+ entropy にし OCI Logging 投入前に既知値を
      チャンク境界跨ぎ込みで redaction する。
    - **full 資格情報隔離が要る場合の option（人間判断 — open-item #1）**: 再利用 genai をログ経路から完全に
      外したい場合は、**別ネットワークエンドポイントのジョブ専用ブローカー**（生成 CI とは別、送信元を当該
      ジョブ CI に限定）を置き、CI は資格情報なしにブローカー経由で LLM/コールバックを呼ぶ構成に拡張できる。
      同居ブローカーは NSG 名前空間共有で無効（R11-F001）なので必ず別エンドポイントにする。採否・配置・
      ライフサイクル・送信元対応付けは NSG 限定の実 CI 検証と併せ open-item #1 の人間判断とする。
    - **AUTH_REQUIRED の位置づけ（多層防御・codex-review R12-F002）**: `AUTH_REQUIRED` は**通常 API プロセスの
      設定**であり CI 側設定ではない。一次制御は上記 NSG-lock（通常 API へ到達させない）。加えて**専用
      コールバック面/通常 API の配備を `AUTH_REQUIRED=true` 必須**とし（jetuse-dev の `AUTH_REQUIRED=false` の
      無条件 dev-user を防御層として塞ぐ）、NSG に漏れがあってもトークン無し要求が 401 になることを負の実 CI
      E2E に含める（open-item #1）。
    - **操作トークンの残余無効化（再掲・補強）**: src_upload/dist_upload（単回消費フラグ）と plan_read/src_read
      （カウンタ上限）は漏れても状態機械（§2）が一回性・順序・相×操作で**リプレイ/横移動を拒否**。
      **`log_append` は再利用可能トークン**だが seq 冪等 + chunk/累積/件数/
      レートの原子上限（§5.5 逐次ログ）で被害面が上限に抑えられ、terminal で全 jti 失効（codex-review
      R10-F005 — log_append を単回消費と誤分類しない）。いずれも短命 exp。
    - **多層防御（主たる制御ではない）**: token は既知プレフィックス（`jetuse-jt-`）+ 十分な entropy にし、
      OCI Logging 投入前に既知値を**チャンク境界跨ぎ込みで完全置換**する。
    - **受け入れ条件（負の E2E）— 2 つを分けて書く（codex-review R13-F003 — 正本では相1 CI が再利用 genai
      トークンを保持するため「非信頼コンテナに資格情報が存在しない」は達成不能）**:
      1. **OCI 署名鍵・RP 資格情報が非信頼コンテナに存在しない**（`is_resource_principal_disabled=true`・RP 環境
         変数不在・RPST 取得失敗）を実機確認する（これは達成可能で正本と整合）。
      2. **再利用 genai ジョブトークンは CI に存在するが、漏洩時の到達元・予算・期限が制限される**ことを実機確認
         する — コールバック面 ingress を生成 CI 送信元に制限し、別送信元からの漏洩 Bearer が拒否される／予算
         超過が 429／exp 後が 401 になる負の E2E（open-item #1）。
      **もし「再利用トークン自体を CI に存在させない」を要件とするなら、別エンドポイントのジョブ専用ブローカーを
      option でなく必須化する**（人間が open-item #1 で判断）。
  - 回収順序: reconcile は「OCI Logging 回収（redaction 済み）→ 台帳/ログ確定 → CI 削除」の順を守る。
    kill / クラッシュ / 通信断 / 起動失敗の各故障注入で保存範囲と redaction を確認するテストを受け入れ条件に。

### 6. OpenCode headless 実挙動・生成品質（findings と設計への反映）

- **成立**: `opencode run`（headless）+ openai-compatible provider + 大阪モデルで、プラン →
  3 ブロック実装 → `npm run build` 緑まで自走（gpt-oss-120b で 2/2。生成物の静的検査違反ゼロ）。
- **モデル対応の実測**: chat.completions 疎通可 = openai.gpt-oss-120b / 20b・
  google.gemini-2.5-flash・meta.llama-3.3-70b-instruct。**不可 = cohere.command-a 系**
  （`400 Unsupported OpenAI operation`）。ただし**生成の完走まで実証できたのは gpt-oss-120b のみ**:
  gemini-2.5-flash は同一プラン・同一プロンプトで **15 分タイムアウト・成果ゼロ**（agentic ループが
  進行しない — verification gen2）。疎通可 ≠ 生成可、がこの検証の重要な帰結。
- **既定モデル = `openai.gpt-oss-120b`**。**使用モデルは設定で切り替え可能にする（specs/19 §4.1 F2 —
  spec 承認条件）**: `Settings.generation_model`（env `GENERATION_MODEL`）を単一真実源とし、
  ジョブ起動時に opencode.json（provider 宣言 + `model`）へ展開する。**同じ allowlist 値が設定検査と
  プロキシ双方へ届くよう、env 名は参照実装のプロキシと一致させ `GENAI_MODEL_ALLOWLIST` に統一する**
  （codex-review R12-F004 — ADR/実装で名前がずれると非既定モデルがプロキシに伝わらず 403）。この allowlist に
  含まれない設定は生成開始時に fail-closed（`failed` + config.generation.error）。非既定の有効モデル設定時に
  設定検査とプロキシ allowlist の双方へ同じ値が届く契約テストを受け入れ条件に含める。**allowlist の既定は実証済みの
  `["openai.gpt-oss-120b"]` に絞る**（gpt-oss-20b / llama-3.3-70b は疎通のみ確認 — 追加時に
  フル生成を再計測して広げる。機構が設定なので運用で広げられる = F2 充足）。
  N6 の `config.frontend.generator.model` に記録。
  - **生成モデル（上記・OCI モデル id `openai.gpt-oss-120b`）と生成 SPA の実行時モデル（API MODELS の
    公開キー `gpt-oss-120b`）は別名前空間**: 実行時に生成 SPA が能力 API へ渡すモデルは**信頼ビルド相が
    ビルド時定数（Vite `import.meta.env.VITE_DEMO_MODEL`）として焼き込む**（client.js）。CSP
    `default-src 'self'` はインライン script を許さず `window.__DEMO_CONFIG__` 方式は黙って既定へ落ちるため
    不採用（codex-review R9-F008）。**ビルド設定は MODELS の公開キーのみ受理し、プラン能力との互換
    （chat 系は rag=true 非対応 等）も検証**する（codex-review R11-F002 — OCI id を焼くと 400 unknown model）。
    受け入れ条件は grep でなく**実配信 CSP 下で非既定モデルの実 chat/rag リクエストが成功する契約テスト**。
- **headless の落とし穴**: permission 未設定の `opencode run` は承認プロンプトで無限ブロックする。
  生成イメージの opencode.json に `permission: {edit: allow, bash: allow, webfetch: deny}` を焼き込む。
- **品質の限界（正直な記録)**: 「ビルド成功 ≠ 実行時正しさ」。**成功率は build 成功率と実行時契約成功率を
  分けて数える**（codex-review R11-F008 — build 緑でも実行時契約は別）。**3 回の生成で 3 件とも実行時
  React state バグ**が独立に混入した — gen1（旧 client.js）は古い closure 変数を append、gen5（現行
  client.js）は初回 delta で user 発話を履歴から削除（`prev.slice(0,-1)`）、gen6 は chat() の戻り値を使わず
  stale な chatStreaming を履歴へ追加し応答が空になる（かつ AGENTS.md の明示指示にも不従）。client.js の
  改善で個別バグは移り変わるが**実行時契約の成功例はゼロ**（build/静的検査は 3/3 通るのに実行時 state は
  3/3 誤る）= **生成品質は本質的に不安定**。「動く SPA」は固定 UI 部品または複数ターン契約モックが通るまで
  成功例に数えない。緩和は
  (a) スキャフォールドの
  固定 API クライアントに加え**ブロック UI 部品も同梱**し、生成を「プラン → 部品の配線」に寄せて
  自由記述面を縮める（実装時に段階導入）、(b) ビルダー UI のプレビュー確認（specs/19 §7 手順 4）を
  人間ゲートとして機能させる、(c) 再生成（failed→provisioning の冪等上書き）を安価に保つ。

## 実測サマリ（詳細は docs/verification/SP3-03.md）

| 実験 | 結果 |
|---|---|
| 署名プロキシ経由 chat.completions / SSE | 200 / 0.3 秒・SSE 素通し成立 |
| OpenCode headless（ホスト・gpt-oss-120b） | プラン→SPA→build 緑 **24 秒**・RSS 614MB |
| OpenCode headless（コンテナ・DNS 遮断・2cpu/2GB/pids512・鍵レス） | 完走 **3 分 26 秒**・静的検査違反ゼロ |
| **提案シェイプ相当 1cpu/4GB ×3（同ロックダウン）** | **3/3 成功: 95s / 165s / 96s（中央値 96 秒）**・静的検査違反ゼロ |
| オフラインビルド（--network=none・vendored lockfile 固定） | 成功・exit 0（offline-build.log） |
| 信頼ビルド相（RO root + RO node_modules・dist/tmp のみ tmpfs） | build 成功 577ms・保護ファイル書込は全て EROFS 拒否（trusted-build.log） |
| 生成相 build-free（AGENTS.md 更新後） | dist 未作成=src のみ・保護ファイル無改変（gen6-buildfree.log） |
| Container Instance 起動（実 CI・使い捨て・DELETED 終端まで証跡化） | **43 秒**で ACTIVE |
| モデル切替（同一プラン・config 差し替えのみ） | gemini-2.5-flash: 15 分 timeout・成果ゼロ / cohere.command-a: 400（chat.completions 非対応） |

## 代替案と棄却理由

| 案 | 棄却理由 |
|---|---|
| A1: API コンテナ内 subprocess | S1 が構造で言えない（API の RP トークン・DB 接続と同居）。N7 が API と cgroup 共有。プレビュー API は 1 OCPU/4GB で生成の 614MB+build が本体を圧迫 |
| A2: OKE Job | クラスタが存在しない（再構築は本タスクの範囲外の課金・運用判断）。将来 OKE 復帰時は「CI 起動」を「Job 起動」に差し替えるだけで他の決定は不変 |
| A3: 常駐ワーカー | 生成間の使い捨てが弱い・常駐コスト。SA 個人規模で不要 |
| B2: OpenCode へ OCI 資格情報を直接渡す | S2 違反（サンドボックス内に署名鍵）。OpenCode に IAM 署名のプラグイン点が無いことも確認済み |
| C2: npm レジストリ egress 許可 | 供給網リスク + egress 面の拡大。C1 が実測成立したため不要 |
| 成果物受け渡しに書き込み PAR | URL 所持 = 権限の面が増える（§5.2 の PAR 不採用と同根）。egress 許可先も 2 点に増える |

## リスクと残課題（実装時に検証）

> **承認をゲートした未解決点（2026-07-08 に人間決定で解消 — 上「人間決定記録」参照）**:
> **【決定済み】** 3 点とも **Internal 限定前提・PoC-first でリスク許容**。#1 = 完全隔離不要（シンプル到達構成・
> ブローカー不要）、#2 = 自由 JSX + 同一オリジン継続（既存の CSP/静的検査/固定クライアントは維持）、
> #3 = PID/ディスク未検証で許容（CPU/メモリ + 15 分キル + N2 検査で足りる）。将来顕在化時は OKE 移行 +
> audit 充実で対応。以下は判断の背景（実装時の緩和理由の記録として残す）:
> 1. **生成 CI のネットワーク隔離（NSG による専用コールバック面への宛先限定 + OCIR pull/egress 分離 +
>    API 側 AUTH_REQUIRED=true）**（§3・§5.5(a)・codex-review R7-F002 / R11-F001 / R12-F002）が実 CI で
>    成立するかは、本フェーズ（podman 近似・承認前）では検証できない。実 private OCIR イメージ + 想定 NSG の
>    実 CI 実験が必要: (i) pull 成功後にコンテナから OCIR/他サービス/直 IP/RPST がいずれも失敗、
>    (ii) 生成プロセスが**通常 `/api/*` へ到達できない**: 専用コールバック面を**通常 API とは別の L3/L4
>    エンドポイント（private IP/LB/VNIC）**に置き（NSG は URL パスを識別できないためパスでなく宛先で分ける —
>    R13-F002）、生成 CI の egress をその 1 宛先に限定 + **コールバック面 ingress を生成 CI 送信元に制限**する
>    （漏洩 Bearer を別送信元から拒否）。同居コンテナは名前空間共有で迂回可のため隔離は L3/L4 宛先分離で行う。
>    (iii) **通常 API/コールバック面の配備が `AUTH_REQUIRED=true`**（多層防御。`AUTH_REQUIRED` は API プロセス
>    設定であり CI 設定ではない。jetuse-dev の `AUTH_REQUIRED=false` = 無条件 dev-user では NSG 漏れ時に横移動可）。
>    以上を負の実 CI E2E（外部・別 CI・通常 API への接続拒否、別送信元からの漏洩 Bearer 拒否）で確認する。
>    **この L3/L4 分離 + ingress 制限が実 CI で成立しない、または再利用 genai をログ経路から完全に外す full 隔離を
>    求めるなら、別ネットワークエンドポイントのジョブ専用ブローカーを option でなく必須にする**。ネットワーク/
>    NSG/IAM に踏み込むため人間ゲート。**成立しなければランタイム B' の再検討が要る**（例: OKE 復帰 + NetworkPolicy）。
> 2. **S3 の完全な fail-closed は「自由 JSX 生成」とは両立しない + 生成 SPA の同一オリジン相互隔離**
>    （§4.3 の S3(a) 補強・§3.5・codex-review R8-F002 / R13-F001）:
>    (a) CSP は `window.location`/`window.open` 等の navigation 経由の外部送信を止められない。真に fail-closed に
>    するには生成面を**宣言的設定（固定部品への配線）に狭める**設計判断が要る。
>    (b) **同一オリジンに全デモを載せると、デモ A の生成 JS がデモ B の能力 API（`/api/demos/B/...`）を叩ける**
>    （path 別 HttpOnly Cookie も固定ヘッダ `X-JetUse-App` もブラウザ/生成 JS が満たすため、A と B 双方を開いた
>    利用者でデモ間横断が成立 — R13-F001。「別 demo Cookie 拒否」テストではこのブラウザ挙動を検出できない）。
>    真の隔離には**デモごとの別オリジン**（サブドメイン等）または **opaque-origin sandbox + 明示セッション能力**が
>    要る。specs/19 §4.3 脅威モデルは完全封じ込めを求めていない（権限昇格なし・CSP 主）ため、**「自由 JSX +
>    CSP/静的検査（=行儀+外部接続遮断）+ 同一オリジン」で SP3 を進めるか、宣言的生成 + 別オリジン隔離へ倒すか**を
>    判断されたい。負の E2E（**A/B 双方の Cookie があるブラウザで A から B の能力 API を呼ぶ**）を受け入れ条件に。
> 3. **N7（プロセス数・ディスク上限）の強制方式が OCI Container Instance で成立するか**（§5・codex-review
>    R11-F004）: CI の resource_config は CPU/メモリのみで、PID/ディスク上限の強制方式が未定。**強制不能なら
>    ランタイム B' 自体が成立しない**（暴走生成を封じ込められない）ため、実装後回しでなく方式選定を左右する
>    未解決点。実 CI で PID/2GB 境界内成功・超過時 failed + CI 削除を承認前に検証するか、不可能なら B' の
>    代替（OKE + cgroup/quota）を判断対象にする。
> adr_approval はこの 3 点の可否判断を含む。

- **S3(b) 静的検査は素朴な「絶対 URL = 不合格」では成立しない（実測 findings）**: React/ReactDOM の
  minified バンドル自体に定数 URL（reactjs.org の error-decoder・w3.org の XML namespace 6 種）が
  埋まっており、スキャフォールドのクリーンビルドさえ落ちる（証跡 offline-build.log 末尾）。
  実装は**二層で fail-closed** にする（単なる許可リストにしない — 許可済み URL を生成コードが
  fetch/navigation に再利用する抜け道を残さないため）:
  - **層0（前提）: 生成物は src 差分のみ・保護ファイルは原本のまま（§1 の 2 相分離が担保）** —
    検査/ビルド相は生成 src 差分を **`src/` 配下にのみ**重ね、保護ファイル（`src/api/client.js`・
    `package.json`・`package-lock.json`・`vite.config.js`・`index.html`）と node_modules は**信頼
    イメージの原本を使う**（生成相の改変は差分の適用時点で `src/api/` 等の保護パスへの書き込みを
    拒否して落とす — gen3 の npm install 逸脱が示すとおり指示遵守は安全境界にならないため、機械的に強制）。
    保護パスへの書き込みを試みる悪性 fixture が公開に到達しないことを単体テストで担保（受け入れ条件）。
  - **層1（正・例外なし）: 生成 src 差分のビルド前検査** — 適用後の生成対象ソース（`src/` 配下で
    保護ファイルを除く全ファイル）に対し specs/19 §4.3 S3(b) を **例外ゼロ**で適用する
    （絶対 URL・プロトコル相対・スコープ外 `/api/`・保護 client.js 以外の生 fetch/XHR/WS/ES → 不合格）。
    生成コードが React 由来 URL を書いてもここで落ちる。
  - **層2: バンドル検査** — ビルド後の全ファイルを走査し、絶対 URL は**由来を検証できる集合**
    （サーバ自身が同一スキャフォールド + プレースホルダのみで行うクリーンビルドから機械的に導出
    した定数集合 = 生成コードを一切含まないビルド由来であることが構造的に保証される）に一致する
    もののみ許容。それ以外は不合格。秘密パターン（S4）はどちらの層でも例外なし。
  - 両層とも不合格 = `failed`（S5 のポインタ切替に到達しない）。この二層方式は specs/19 §4.3
    S3(b) の「許可リスト方式: 相対参照以外は原則落とす」の**適用細則**であり、
    **adr_approval の承認対象に含める**（spec 本文の字義（バンドル全ファイルで絶対 URL 不合格）
    だけでは React を含むあらゆるバンドルが不合格になるため、細則なしに S3(b) は実装できない。
    承認時に specs/19 §4.3 S3(b) へ本細則の参照を追記する — spec 改訂も人間ゲート内）。
- **S3(a) は字面検査だけでは構造的に成立しない（codex-review F003 — 正直な限界と補強）**: 静的検査は
  `fetch(`/`XMLHttpRequest`/`WebSocket(`/`EventSource(` と絶対 URL の**字面**しか捕まえられず、生成コードは
  実行時に URL を組み立てて `form.submit()` / `window.location` / `<a target>` / 動的 `import()` で
  データを外部送信しうる（`connect-src 'self'` は接続系を止めるが **navigation/form は止めない**）。
  よって S3(a) は次で補強する（**adr_approval の承認対象** — specs/19 §4.3 S3(c) の CSP と §4.5 の
  生成面の狭め方を更新する）:
  - **CSP を強化**: specs/19 §4.3 S3(c) の CSP に **`form-action 'self'; base-uri 'none';
    object-src 'none'; frame-ancestors <親のみ>`** を加える（navigation/form/base 差し替え/プラグインを
    ブラウザ側で塞ぐ）。
  - **CSP では navigation 経由の外部送信を止めきれない（codex-review F002 — 本質的限界）**:
    `window.location` / `window.open` / 外部リンクによる top-level navigation は CSP のどの指令でも
    確実には塞げない（`navigate-to` は仕様撤回・未実装）。任意 JSX を生成できる限り、生成コードは
    動的に組んだ URL へデータを載せて navigation で送出できる。したがって **CSP 強化だけでは S3 を
    真に fail-closed にはできない**。
  - **真の fail-closed には生成面を「宣言的設定」に狭めるしかない（設計判断 — 承認ゲート）**: 自由 JSX を
    やめ、生成物を**プランのブロック → 固定部品への宣言的配線（JSON 相当）**に限定し、任意の JS/navigation を
    書けなくする（specs/19 §7 の「プラン JSON の自由編集をさせない」と同方向、§4.3 脅威モデルの
    (i) 行儀の担保とも整合）。これは生成アプローチを変える**設計判断**であり、SP3-03 では自由 JSX 生成が
    実機成立している（gen1/5/6）ため、**「自由 JSX + CSP/静的検査（≒ 行儀 + 外部送信の CSP 遮断・完全な
    封じ込めではない）」で SP3 を進めるか、宣言的生成に倒すか**を adr_approval の判断項目に上げる。
  - **正直な位置づけ**: specs/19 §4.3 の脅威モデル注記自身が「生成 SPA は利用者自身の権限で動く=権限昇格
    にならない。S3 の目的は (i) 行儀の担保・(ii) 外部送信の遮断（CSP が主）・(iii) SP4 の先取り」と
    **完全封じ込めを要求していない**。本 ADR も自由 JSX を許す限り「構造的に client.js のみが通信する」
    とは主張せず、CSP（form-action/base-uri/object-src/frame-ancestors + connect-src）で
    外部**接続**とフォーム/フレーム/base を塞ぎ、navigation 経由の残余リスクは上記設計判断で扱う、を正とする。
    可能なら層1/層2 を AST ベースに上げ動的 URL 構築・navigation・form 生成を拒否する（緩和）。
- **AGENTS.md の指示遵守はモデル任せにしない**: gen3 で OpenCode が指示（npm install 禁止）に反して
  `npm install` を実行した実例あり（オフライン環境のため実害なし = 変更ゼロで成功扱い）。禁止事項は
  プロンプトでなく**環境で強制**する（egress 遮断・lockfile 固定・生成後の lockfile/package.json
  改変検査で fail-closed）。
- **プロキシ成果物エンドポイントの上限・検証**: サイズ 20MB（N2）・パス正規化・ファイル数上限は
  受領時に fail-closed（実装時に単体テスト）。
- **固定 API クライアント（client.js）の契約テスト = 昇格時の受け入れ条件**（codex-review R12-F006 —
  実行時契約成功が 0/3 で回帰検出手段が無い）: スキャフォールドを実装へ昇格する際、fetch/ReadableStream を
  モックした自動契約テストで **`deriveBase` の境界（/app・/app/・/app/foo/）・分割 SSE・`[DONE]`・error
  イベント例外化・破損 JSON・途中 EOF・reader 解放・dbchat の nl2sql→execute 二段**を網羅する。
- **CI 起動の失敗モード**（容量不足・イメージ pull 失敗）: `failed` + reconcile で収束（§4 決定 4）。
- **生成イメージの配布**: OCIR（既存 ADR-0011 の流儀）。イメージビルドは CI/CD 外で ops スクリプト
  （初版は手動ビルド + push — 人間ゲート内）。
- **モデル多様性が現状 1 モデル**: 完走実証が gpt-oss-120b のみで、同モデルの品質退行・提供停止が
  単一障害点。緩和 = allowlist 機構（設定で追加可能）+ 追加候補（gpt-oss-20b / llama-3.3-70b）の
  フル生成再計測を実装フェーズの残課題にする。
- 上流 `GET /openai/v1/models` が 404 に変わっていた（上流仕様変動）。OpenCode はモデルを config
  宣言で使うため影響なしだが、**上流互換面の変動リスク**として tips に記録。
- **将来方向（2026-07-08 人間決定 — 承認済み緩和の顕在化時の対応）**: 本 PoC は Internal 限定前提で
  (1) 生成 CI の完全ネットワーク隔離、(2) 生成 SPA の同一オリジンによるデモ間隔離、(3) N7 の PID/ディスク
  強制、を**リスク許容**して進める。これらが顕在化（マルチテナント公開・悪性利用・暴走生成の実害）した際は、
  **① Container Instances → OKE 移行**（NetworkPolicy による厳密なネットワーク隔離・cgroup/quota による
  PID/ディスク強制・namespace/別オリジン分離）と、**② 監査（audit）の仕組みの充実**（生成・配信・能力 API
  呼び出しの owner 紐付けログ・異常検知）で対応する。PoC 段階ではこの 2 方向を**将来の受け皿**として残す。

## 承認後の実装スコープ（tasks/SP3-03.md 本体）

specs/19 §4.5（generate API）・§4.3（安全ゲート）・§5（保管・配信・config・後始末 3g)は本 ADR の
方式で実装する。本 ADR が specs/19 の要件枠（S1〜S6・N1〜N7・F1〜F6）を変えることはない。
**承認済み緩和（Internal 限定・PoC-first）**により、NSG 負の E2E・別オリジン隔離・PID/ディスク強制の境界 E2E は
実施しない（SKIPPED.md に理由明記）。モデル切替可能（F2）は実装必須。

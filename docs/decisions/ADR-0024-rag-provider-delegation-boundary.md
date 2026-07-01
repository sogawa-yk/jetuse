# ADR-0024: RAG Provider Adapter の委譲境界と config 解釈（施主承認済み 2026-07-01）

- 状態: **Accepted（施主承認 2026-07-01）**。決定1–5 を承認（resolver 実装・共有ポリシー・fail-closed 認可を含む）。
  - **施主による上書き（topK）**: 下記「決定3」は draft 時点で「configSchema に上限を持たない（honor する）」としていたが、
    施主判断により **`retrieval.topK` に上限（`maximum`）を追加**する（無上限は巨大 prompt/コスト footgun のため）。
    契約正本 `answer-with-citations.config.schema.json` へ `maximum` を追加し、Provider/backend 境界でも上限超過は
    明示エラーで拒否する（暗黙クランプはしない）。＝契約所有者（施主）承認済みの契約変更。
- 日付: 2026-07-01（起票・承認）
- 関連: [ADR-0021](./ADR-0021-asset-onboarding-seam.md) / [ADR-0022](./ADR-0022-experience-builder.md) /
  タスク EXB-04 / `specs/17-experience-builder/`
- 起票: stage-runner / stage-1 / EXB-04（`answer.with-citations@1` の実 RAG 委譲で、spec に無い
  config 解釈・委譲先選択の判断が必要になったため、CLAUDE.md の spec-driven 原則により人間レビューを要求）

> **実装との関係（spec-driven の遵守 / Codex EXB04-007 反映）**: 本 ADR が Proposed の間、コードは
> **spec 逸脱の判断を稼働させない**。すなわち (a) EXB-04 の受け入れ条件が明示要求する範囲（Provider Adapter
> 本体・`generate` 系への委譲・標準イベント整形・Empty 経路）は実装するが、(b) **spec 外の判断である
> 「共有/curated KnowledgeSpace → 別 owner の写像」は実装せず、アクセス確認済み `resolve_owner` resolver が
> 注入されるまで `PermissionError` で拒否する**（＝未実装・fail-closed）。バンドルされる既定挙動は
> `space == principal`（自分の Knowledge のみ）で、これは既存 `/api/chat`（owner=認証済み subject）と同一の
> **spec 準拠**動作であり spec 外判断ではない。したがって「仕様外判断は実装せず ADR 案に留める」原則は満たす。
> 下記「決定」は resolver 実装・共有ポリシーを**含めて**承認を求めるものであり、承認までその経路は無効。

## コンテキスト

EXB-04 は `answer.with-citations@1`（`rag.answer`）の実 RAG 実行を、既存 jetuse_core の実機検証済み RAG へ
**委譲**する Provider Adapter を実装する（実装方針 §3.5 / §12.2、ADR-0021 seam）。Stage 0 契約
（`answer-with-citations.config/input/output/event`）は正本だが、次の2点は spec に明記が無く実装判断を要する。

1. **config の `knowledge.space` を jetuse_core のどの識別子に写像するか。**
   契約上 `knowledge.space` は「参照する KnowledgeSpace の論理名（+ 任意 version）」（`specs/17` §config）。
   一方 jetuse_core の RAG は **`owner`（subject）単位**で Vector Store / 索引を分割する
   （`rag.ensure_store(owner)` / `generate(owner, prompt)`）。両者を繋ぐ写像が spec に無い。

2. **どの jetuse_core RAG 実装に委譲するか。**
   jetuse_core には 3 経路がある: ①`chat.stream_chat` + file_search（vector_store）②`rag_select_ai.generate`
   ③`rag_opensearch.generate`。②③は `(owner, prompt) -> (answer, citations)` の同一シグネチャで、既存 chat
   ルートの RAG ディスパッチが共用する。契約が要求するイベント順序は
   **`retrieval.started` → `retrieval.completed`(citations) → `message.delta`(text)**（retrieve→generate の
   2 相・引用が本文より先）。

## 決定（提案・人間承認待ち）

1. **`knowledge.space`→`owner` 写像は fail-closed とし、認可を必須にする（Codex EXB04-001 反映）。**
   KnowledgeSpace ＝ RAG ストアの所有者パーティションと解釈するが、config で任意 space を指定できるだけの
   seam は既存 `/api/chat`（owner=認証済み `user.subject`）の認可境界を迂回して**別 owner の索引を参照**できる。
   よって Adapter は:
   - 認証主体 `principal` を**必須引数**で受け取る。
   - 共有/curated Knowledge（`space != principal`）は、**アクセス確認済みの `resolve_owner` resolver が
     注入されたときだけ**許可する（`(space, version, principal) -> owner`）。
   - resolver 未注入かつ `space != principal` は `PermissionError` で**拒否**（自分の Knowledge のみ）。
   これにより「Builder が curated space を束縛し Demo User が引く」ユースケースは、**アクセス制御込みの
   KnowledgeSpace レジストリ（resolver 実装）と本写像の承認**という人間ゲートを通した上で成立する。
   レジストリ実装・共有ポリシーの承認は本 ADR の未承認事項（下記）。`version` は resolver に委ね、resolver
   未注入時に指定されたら拒否する（版固定を黙って無視して別版へ回答しない）。

2. **委譲先は `generate` 系（`rag_select_ai` / `rag_opensearch`）とし、backend は差し替え可能にする。**
   理由: `generate` は retrieve+generate を実行し `(answer, citations)` を原子的に返すため、契約の
   **引用→本文**順序（`retrieval.completed` を `message.delta` より先に発行）を自然に満たす。file_search 経路は
   Responses ストリームの構造上 citations が完了時（本文 delta の後）に確定するため、契約順序に整形するには
   OCI Responses を Adapter 内で直叩き＝「既存 RAG を書き直さない／OCI 直叩き禁止」に反する。よって MVP は
   `generate` 系へ委譲する。**既定 backend は持たない（Codex EXB04-011 反映）**: `CoreRagAnswerProvider` は
   `backend=`（`select_ai`/`opensearch`）指定か delegate 注入を必須とし、どちらも無い構築は `ValueError`。
   select_ai は ADB、opensearch は任意機能（cluster）を要し、いずれも無条件既定にすると未構成環境で必ず失敗
   するため。どの backend が構成済みかは環境（EXB-03/settings）が決める。**回帰比較（EXB-04 シナリオ3）は
   同一 delegate（例 `rag_select_ai.generate`）を Adapter と既存 chat ルートの双方が呼ぶため、委譲による退行が
   構造的に発生しない**ことを証跡で示す。

3. **`message.delta` は完了本文を分割して逐次発行**する（既定 200 字）。`generate` は本文を原子的に返すため、
   契約の「逐次 text」を満たす最小整形として分割する。**`retrieval.topK` は黙殺しない（Codex EXB04-002 反映）**:
   topK 対応 backend（opensearch）へは `top_k` を伝播し search の `k` に反映、非対応 backend（select_ai narrate は
   件数を外部指定不可）へ topK が来たら**明示的に拒否**する（指定が効かないまま回答するのを防ぐ）。委譲先
   `generate` には後方互換の optional `top_k` キーワードを足す（既存 2 引数呼び出しは不変。`top_k<1` は拒否）。
   **topK の上限**は configSchema が `>=1` のみで上限を持たない（正本を EXB-04 では変更しない）。topK は
   **Builder が束縛する config 由来（§7.1 ＝ 実行時ユーザー入力ではない信頼値）**であり、backend 側
   （OpenSearch は `index.max_result_window`）が結果件数上限を持つため、**アプリ層で丸めず値どおり honor** する
   （クランプは schema 有効値の暗黙改変になるため採らない。Codex EXB04-006/016/019/026 の整理）。

4. **seam は EXB-03 が実体化した `RunProvider` に適合させる（実装方針 §8.1 の実現形）。**
   §8.1 `CapabilityProvider` は概念契約で、**merge 済の EXB-03（`service/runs.py`）が同期
   `RunProvider.run(ctx) -> Iterator[dict]` として実体化**した。よって `CoreRagAnswerProvider` はこの
   `RunProvider` を実装する: capability 固有イベント（`retrieval.started`/`retrieval.completed`/
   `message.delta`）の dict を yield するだけで、**lifecycle（run.started/completed/failed）・出力組立
   （delta 累積→answer, retrieval→citations）・イベント順序検証・(将来の)cancel は engine が所有**する。
   `principal = ctx.owner_sub`、config は `ctx.config`（Experience 束縛。MVP 未束縛時は自分の Knowledge
   =`space=owner_sub`）。**配線は config-gated**: backend が settings で構成済みなら実 `CoreRagAnswerProvider`、
   未構成なら `StubProvider`（`service/runs.py` の `_select_provider()`）。当初 draft の async
   `start/resume/cancel` 形は EXB-03 の実 seam に置換した（両契約=RunProvider と answer-with-citations を壊さない）。

5. **Empty（ヒット無し）は空 citations ＋「該当なし」系本文で正常終了**する（`run.failed` にしない）。
   backend が本文を返せば尊重し、空なら既定文言（「該当する情報が見つかりませんでした。」）に置換する。
   委譲先が例外を送出した場合（＝真の失敗）は握り潰さず伝播し、EXB-03 の Run 層が `run.failed` へ写像する。

## 帰結

- 生成 UI / 新 API は OCI を直叩きせず、実機検証済み jetuse_core RAG を Adapter 経由で再利用できる（ADR-0021）。
- `knowledge.space`→`owner` 写像により、単一ユーザーの RAG ストアを「KnowledgeSpace」として Builder が束縛する
  運用に閉じる。**マルチスペース・space の版固定・アクセス制御は本 ADR の範囲外**（後続で拡張）。
- backend を `generate` 系に限定するため、file_search（vector_store）固有の逐次 citation は MVP では使わない。
  file_search 経路での契約順序整形が必要になれば、jetuse_core 側に「retrieve のみ」API を足す別タスクとする
  （本 Adapter は書き直さない方針を維持）。

## 未承認事項（人間ゲート）

- 本 ADR の Accepted 化（施主承認）。承認まで状態は Proposed。
- `knowledge.space`→`owner` 写像の運用妥当性（curated Knowledge をどの subject 配下に置くか）。

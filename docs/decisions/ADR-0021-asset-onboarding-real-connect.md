# ADR-0021: 既存資産オンボードの実接続（external-app SSO / asset connector 実 MCP / marketplace 流通）

日付: 2026-06-29
状態: **承認済（2026-06-29 施主承認）**。BE-06 で起票。ADR-0015 §8「既存資産オンボードは ASSET-01 で
別 ADR 追補」の追補。seam（継ぎ目）方式で実接続を配線し、**実接続の活性化（実 IdP/Vault/Identity Domain/
外部資産/実 MCP の実値注入）のみを人間ゲート**とする方針が承認された。0018=mcp-auth / 0019=BE-04 /
0020=BE-03 が使用済みのため本 ADR の番号は 0021。

> ASSET-01 は `kind: external-app` / asset connector（No.1-RAG / No.1-SQL-Assist）の **配布表現（manifest /
> 定義）と in-process の決定的ブリッジ**までを確定した（specs/16-platform.md §14）。本 ADR は BE-06 で
> その先＝**実接続の配線**（実 token-exchange / 実 MCP invoke / marketplace external-app 流通）を、人間
> ゲート（実 IdP・外部資産・Vault・Identity Domain）を越えない範囲でどこまで自動化するかを確定する。

## 背景

ASSET-01 完了時点の制約:
- `external_app.build_sso_handoff` は **決定的・オフライン**（IdP へ通信せず）で RFC 8693 要求の shape を
  参照名のまま組み立てるのみ。実 token-exchange の実行経路・SSO 起動ルートが無い。
- `asset_connectors` は配布表現の正規化のみ。**呼出元が存在せず**、実 MCP への invoke 経路が無い。
- `marketplace.SUPPORTED_KINDS` は external-app を除外（§14.4 で「後段」とした。store/migration 無し）。

「実接続」には実 IdP（Identity Domain=テナンシ変更）・実 client_secret/API トークン（Vault）・実 MCP
エンドポイント配備が要る。これらはいずれも **人間ゲート**であり、エージェント自走では越えない。一方で
「実値が揃えば実接続できる配線」は自走で実装でき、mock で検証できる。ここで決めるのはその境界である。

## 決定（案）

1. **継ぎ目（seam）方式で実接続を配線し、実値の注入だけを人間ゲートにする**。CON-02
   （connector_runtime）の `secret_resolver` / `mcp_caller` 方式を踏襲し、external-app SSO も同型にする:
   - `external_app.exchange_sso_token(...)` を新設。`build_sso_handoff` の shape を実値で具体化し
     **実 RFC 8693 token-exchange を実行**する。継ぎ目は (a) `secret_resolver`（secretRef→実 client_secret。
     Vault 束ね）、(b) `subject_token`（利用者セッションの実 id_token。ランタイム引数・非保存）、
     (c) `token_exchange_caller`（実 IdP への HTTP。**既定 fail-closed**）。本番 caller
     `http_token_exchange_caller`（timeout/OAuth エラー正規化）も提供する。
   - asset connector は `invoke_no1_rag_search` / `invoke_no1_sql_nl2sql` を新設し、CON-02 の
     `invoke_connector_action`（mcp transport）へ委譲する（**呼出元**を与える）。`vault_secret_resolver`
     （secretRef→OCID→`mcp_servers._read_secret`）で Vault 解決の継ぎ目を作る。
2. **SSO 起動ルートを公開する**（`/api/external-apps`）。一覧と、決定的・オフラインの handoff shape を返す
   `POST .../sso-launch`、実 exchange を行う `POST .../sso-exchange`。後者は (a) tokenEndpoint、
   (b) Vault secret（`external_app_secret_ocids`）、(c) 利用者の実 id_token（Bearer）、
   (d) 発行 id_token 検証用 JWKS（`denpyon_jwks_url`）の **4 条件が揃わなければ fail-closed**
   （503/401）。＝実 IdP/Vault 未構成では実行されない（人間ゲートを構造で担保）。
   - **install の可視性は platform-wide（BE06-REV-005）**: marketplace install は署名検証済み・運用者
     ゲートで `(plugin_id, version)` が**全体一意**（connector/usecase/sample-app と同一モデル）。よって
     external-app instance の起動導線も **全利用者共通に可視**とし、per-user 限定にはしない（最初の利用者
     だけが使える/同版を別利用者が再 install できない、という多利用者破綻を避ける）。`registered_by` は
     監査用途の任意フィルタであって分離保証ではない。脅威モデルは「任意利用者の勝手な install」ではなく
     「運用者が署名検証済み資産を platform に install」である。
   - **SSO ログイン遷移の引き渡し（BE06-REV-001/BE06-SSO-001/002。実装済み・活性化は人間ゲート）**: ブラウザ
     に id_token を直接返さない（front-channel 漏洩回避）。`sso-exchange` は実 exchange 後に **単回使用・短 TTL
     の handoff code**（`sso_handoff_store`）を発行し `{handoff_code, expires_in}` だけを返す → ブラウザは連携先
     URL へ code を付けて遷移 → **連携先がバックチャネル `sso-redeem`（client_id/secret 認証＋単回使用）で
     id_token を1回だけ受領**（OAuth 認可コード型。再使用・期限切れ・別アプリ流用は fail-closed）。配線・検証・
     単回使用・client 認証は実装＋mock E2E 済み。**活性化**には実 IdP の id_token 発行・実 Vault secret・連携先
     （伝ぴょん）が redeem を呼ぶ実装合意が要るため**人間ゲート**（dev では sso-exchange が 403/503 で発火しない）。
     handoff store はプロセスローカル（in-memory）で、本番マルチインスタンスでは共有ストアへ差し替える（実運用設定）。
3. **id_token を取得する契約**にする（伝ぴょんへ身元を渡す SSO）。`requested_token_type=id_token` を要求し、
   subject は呼び出し側 access token（Web の Bearer。`subject_token_type=access_token`）。
   `build_sso_handoff` が出す shape と `exchange_sso_token` の実要求で **token type を一致**させる。
   - **後方互換（BE06-R005）**: external-app SSO は本タスクで初めて実接続するため released な consumer は無く、
     この token type はここで確定する契約である（shape のみだった ASSET-01 から型を反転ではなく確定）。
     回帰固定のため shape・exchange の双方に token type の単体テストを置く（破壊的変更時は本 ADR を更新）。
   - **発行 id_token の暗号学的検証（BE06-R002/BE06-004）**: `issued_token_type` の自己申告だけでは侵害/設定
     ミスの token endpoint が任意文字列を id_token として通す穴を塞げない。実 HTTP caller 経路は
     **id_token 検証関数（JWKS で署名/iss/aud/exp。`jwks_id_token_verifier`）を必須**にし、未注入なら交換前に
     fail-closed。JWKS 取得は実 IdP 通信＝人間ゲートのため、ルートは `denpyon_jwks_url` 未構成なら 503。
   - **token endpoint 応答は 2xx のみ受理（BE06-R006）**: redirect 無効化時に JSON を伴う 3xx を成功と誤認しない。
4. **秘密の非漏洩を多層で守る**。token-exchange の戻り値・例外から入力 client_secret / subject_token を
   redact し、例外連鎖（`__cause__`/`__context__`）を断つ。secret 解決の例外（未知 ref・Vault 拒否・一時
   障害）は `SsoHandoffError`/`ConnectorInvokeError` へ **連鎖なしで正規化**して Vault 内部情報を出さない。
5. **MCP invoke は最小権限＋呼出検証＋引数照合**。spec を `allowed_tools=[action]` に絞り（broker 認可した
   action 以外を MCP サーバーが公開していても呼ばせない）、既定 caller は `tool_choice=required` を強制し、応答に
   その action の実呼び出しが無ければ fail-closed（別ツール選択・無呼出を `ok` にしない）。さらに応答に
   実引数が載る場合は **認可 payload を完全一致で含むか照合**し、改変/省略を fail-closed にする（BE06-R003）。
   - **既定 caller は fail-closed（BE06-BLK-001/REV-005）**: Responses type:"mcp" はツール引数をモデルが
     生成するため引数照合が **post-hoc**（実行後）になり、改変・越境を実行境界で**事前**に防げない。よって
     `_default_mcp_caller` は **実行せず拒否**する（公開シグネチャは不変＝後方互換は維持。意図的な fail-closed
     化）。安全な事前束縛には Responses を介さない **MCP 直結 transport**（実エンドポイント＝CON-03/人間ゲート）
     が要る。`invoke_no1_*` は `mcp_caller=` 注入で実 MCP を呼ぶ（`_mcp_tool_was_called`/`_args_match_payload`
     は直結 caller の検証部品＝単体テスト済み）。mcp 応答は `ok` の明示を必須にする（暗黙成功にしない）。
6. **marketplace を external-app の install に対応させる**（§14.4 の「後段」を実装）。`external_app_instances`
   テーブル（migration 026。BE-04 が 025 を使用するため繰り下げ）＋ `external_app_store`（register/get/list/remove/delete_by_source）＋
   `installer._ingest_contributes` の kind 分岐を追加し、署名検証・版固定・出所追跡・補償削除の枠組みを
   kind 非依存のまま流用する。**実シークレット値は保存せず**参照名（clientIdRef/secretRef）のみ。install
   済み instance は起動ルートが surface し SSO 起動できる（install→一覧→起動→uninstall が繋がる）。
7. **越えない人間ゲート（最濃）**: 実 IdP 接続・実 client_secret/API トークンの Vault 束ね・実 MCP
   エンドポイント配備・Identity Domain 設定（テナンシ変更）・実 ADB への apply・コミット/PR/push。
   自走は「設計＋配線＋mock E2E」まで（runs/<run-id>/e2e/SKIPPED.md に未実施範囲と再実行手順を明記）。

## 影響

- specs/16-platform.md §14.4 を更新（external-app は marketplace install 対応へ。store/migration の所在を明記）。
- 新規シンボル: `exchange_sso_token` / `http_token_exchange_caller`（external_app）、`invoke_no1_rag_search` /
  `invoke_no1_sql_nl2sql` / `vault_secret_resolver`（asset_connectors）、`external_app_store`、
  routes/external_apps。既存公開シグネチャは後方互換（追加のみ）。
- 新設定: `denpyon_url/issuer/audience/token_endpoint`、`denpyon_jwks_url`、`external_app_secret_ocids`
  （すべて空既定＝機能無効。実値はコミットしない）。

## 代替案

- **実接続まで自走（却下）**: 実 IdP/Vault/外部資産を要し人間ゲートを越える。CLAUDE.md 違反。
- **shape のみのまま据え置き（却下）**: BE-06 の受け入れ条件（実 token-exchange 配線・実 MCP invoke・
  external-app install）を満たさない。
- **external-app を marketplace 非対応のまま据え置き（却下）**: §14.4 の「後段」を BE-06 が解消対象とする。

## 未解決（人間判断）

- 伝ぴょんの OIDC クライアント登録方式（Identity Domain か外部 IdP か）と redirect_uri 許可リスト運用。
- asset connector の実 MCP エンドポイントの配備先（外部資産側 or jetuse 側プロキシ）。
- install 済み external-app の SSO secret を「どの Vault に・誰が」束ねるかの運用（テナント別か共有か）。

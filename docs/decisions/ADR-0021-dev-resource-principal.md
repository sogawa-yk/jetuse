# ADR-0021: 開発環境の DB 内資格情報もリソースプリンシパルへ統一する

日付: 2026-07-29 / 承認: 2026-07-29
状態: **Accepted**（2026-07-29 ユーザー承認。あわせて FIX-59（パーサ修正のみ）の破棄も承認された）
関連: ADR-0020（RAG メタデータ方式）/ `docs/tips.md` 2026-07-28 の項 / SPIKE-M1

## 背景

ADB の中で動く `DBMS_CLOUD` / `DBMS_CLOUD_AI`（Object Storage 読み取り・Generative AI 呼び出し）は、
DB 内に保存された**資格情報オブジェクト**を使う。現状これが版によって二重化している。

| 環境 | 資格情報 | 由来 |
|---|---|---|
| 顧客環境（ワンクリック配備） | `OCI$RESOURCE_PRINCIPAL` | ADB 自身の身分。`infra/orm/locals.tf` が env で注入し、`bootstrap.py` が `DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL` で有効化 |
| **開発環境** | `JETUSE_OCI_CRED` | **開発者の `~/.oci/config` から API キーを抜き出して DB へ焼き込む**（`ops/setup-select-ai.py` / `ops/setup-dev-schema.py`） |

開発側の経路には実害のある不具合がある（`docs/tips.md` 2026-07-28・SPIKE-M1 で発覚）。
両スクリプトの `~/.oci/config` パーサが**セクション（プロファイル）を無視して全行を 1 つの dict に潰す**ため、
複数プロファイルがある環境では**最後のプロファイル**の値を拾う。作られた資格情報は署名形式としては正しく
`CREATE_CREDENTIAL` は成功するが、中身は別テナンシのものになり、以降 `DBMS_CLOUD` の全呼び出しが
`ORA-20404: Object not found`（OCI の NotAuthorizedOrNotFound）になる。

厄介なのは次の 2 点。

- **失敗の見え方が一様**。Object Storage も Generative AI も揃って「見つかりません」になるため、
  バケット名やリージョンを先に疑い、資格情報にたどり着けない（SPIKE-M1 で実際に時間を溶かした）。
- **環境に焼き付いて残る**。間違った資格情報はスキーマ内に残り続け、セットアップを流し直すまで
  ローカル実行だけでなく**配備済みの dev アプリスタックまで巻き込む**
  （`infra/terraform/environments/app` は `SELECT_AI_CREDENTIAL` を注入しておらず、既定の
  `JETUSE_OCI_CRED` にフォールバックする）。

## 実機で確認した事実（2026-07-29・read-only）

当初「リソースプリンシパル化には IAM 整備（動的グループ＋ポリシー）が要り、それは人間ゲート」と
見積もっていたが、**実機を見たところ既に整備済み**だった。

- 動的グループ `jetuse-internal-dg` の照合ルールに
  `resource.type='autonomousdatabase'` が **dev / public / jetuse-test の 3 コンパートメント分**含まれる。
  開発用 ADB は `dev` コンパートメントにあるため、**既にメンバー**。
- ポリシー（tenancy）:
  - `Allow dynamic-group jetuse-internal-dg to manage all-resources in compartment jetuse:dev`
  - `Allow dynamic-group jetuse-internal-dg to read objectstorage-namespaces in tenancy`
  - → Object Storage も Generative AI も `dev` コンパートメント内なら通る。ネームスペース解決も可。

つまり**新規の IAM 変更を伴わない**。

## 決定

**開発環境の DB 内資格情報も `OCI$RESOURCE_PRINCIPAL` に統一し、API キー由来の `JETUSE_OCI_CRED` を作る経路を廃止する。**

具体的には:

1. `ops/setup-select-ai.py` / `ops/setup-dev-schema.py` から
   **`~/.oci/config` を読んで `DBMS_CLOUD.CREATE_CREDENTIAL` を呼ぶ処理を削除**し、代わりに
   `DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(username => <schema>)` を実行する。
2. 開発環境の `select_ai_credential` を `OCI$RESOURCE_PRINCIPAL` にする
   （`infra/terraform/environments/app` の env 注入 ＋ ローカル `.env`）。
3. 既定値（`settings.py` の `select_ai_credential`）をどちらにするかは §「未解決」を参照。

**ローカルの Python プロセスの認証（`AUTH_MODE=config_file`）は変更しない。**
本 ADR が対象にするのは「DB の中で使う資格情報」だけである。

## 却下した案

| 案 | 却下理由 |
|---|---|
| **A: パーサだけ直す**（起票済みだった FIX-59） | 不具合は消えるが、**開発者の API キーを DB へ焼き込む構造が残る**。焼き付いて残る性質も、本番と開発で認証方式が違う状態も解消しない。IAM が既に整備済みと分かった以上、より弱い対処を選ぶ理由がない |
| **B: パスワード・鍵を OCI Vault に置く** | 取得に OCI 認証が要るため、「OCI 認証の設定が原因で DB が使えない」問題の切り分けを難しくする（循環）。開発者 1 人が使う開発用 ADB には過剰。Vault 化は Phase 8 で配備側の秘密管理とまとめて検討する |
| **C: 現状維持** | 同じ不具合を次に踏むのはほぼ確実（RAGM-02 がこの経路を通る）。オンボーディングの初回手順にも含まれており、新規参加者が黙って壊れた環境を作る |

## 影響

- **FIX-59（パーサ修正）は破棄する。** 本 ADR が承認されれば、修正対象のコードごと無くなる。
- 既存の dev スキーマには `JETUSE_OCI_CRED` が残る。**削除は必須ではない**（参照されなくなるだけ）が、
  混乱を避けるため移行手順に「不要になった資格情報の削除」を含めるか判断する（§未解決）。
- 本番（顧客環境）への影響は**なし**。もともとリソースプリンシパルであり、変更しない。
- 開発と本番で認証方式が揃うため、**「ローカルで通るがデプロイで落ちる」種類の差分が 1 つ減る**。

## 未解決（実装タスクで決める / 人間に確認する）

1. **`settings.select_ai_credential` の既定値**を `OCI$RESOURCE_PRINCIPAL` に変えるか、
   既定は現状維持で env 注入に寄せるか。前者は「設定を忘れた環境が安全側に倒れる」利点があるが、
   リソースプリンシパルが使えない環境（IAM 未整備のテナンシへ自前で立てた場合）では起動時に失敗する。
2. **既存 `JETUSE_OCI_CRED` を削除するか**（放置＝参照されないだけ / 削除＝混乱が減るが後戻りしにくい）。
3. **ローカル実行（コンテナ外の Python）から Select AI を使う経路**で、
   `ENABLE_RESOURCE_PRINCIPAL` が未適用のスキーマをどう検出し、どう案内するか（fail-closed の作り方）。

## 「未解決」への結論（2026-07-29 / RP-01 実装時。決定そのものは変更していない）

1. **`settings.select_ai_credential` の既定値 → `OCI$RESOURCE_PRINCIPAL` に変更した。**
   決め手は「安全側に倒れる」ではなく**既定値が指す先が実在しなくなる**こと。本 ADR で
   `JETUSE_OCI_CRED` を作る経路を消したので、既定を据え置くと「誰も作らない資格情報」を
   既定で参照する状態になり、env を書き忘れた環境は確実に落ちる。RP が使えないテナンシへ
   自前で立てる場合は env `SELECT_AI_CREDENTIAL` で上書きできる（上書き手段は残してある）。
2. **既存の `JETUSE_OCI_CRED` は削除しない（自動削除もしない）。** 参照されなくなるだけで
   実害が無く、削除は後戻りできない。混乱を避けたい場合の手作業は
   `BEGIN DBMS_CLOUD.DROP_CREDENTIAL('JETUSE_OCI_CRED'); END;` を当該スキーマで実行するだけ
   なので、ops スクリプトに破壊操作を持ち込むより安い（`docs/verification/RP-01.md` に記載）。
3. **未適用スキーマの検出は「セットアップ時に fail-closed・実行時はヒント」に分けた。**
   `ops/` 側は `ENABLE_RESOURCE_PRINCIPAL` の直後に `DBA_TAB_PRIVS` を引いて
   `OCI$RESOURCE_PRINCIPAL` の EXECUTE が実際に付いたかを確認し、付いていなければ中止する
   （呼び出しが成功しても権限が無い状態を通さない）。ローカル実行側は毎リクエストでの
   事前プローブを増やさず、`nl2sql.create_profile` の失敗ヒントに復旧コマンド
   （`ops/setup-select-ai.py --schema <SCHEMA>`）を明示する形にした。理由は、プローブ自体が
   DB 往復を増やすうえ、失敗は結局同じ 1 箇所に集約して現れるため。

## 検証計画（実機・`docs/verification/RP-01.md` に記録）

1. `ENABLE_RESOURCE_PRINCIPAL` を開発用スキーマへ適用できること（ADMIN 実行）。
2. `OCI$RESOURCE_PRINCIPAL` で **Object Storage の読み取り**が通ること
   （`DBMS_CLOUD.SEND_REQUEST` で既知エンドポイントを叩き 200。従来の切り分け手順と同じ）。
3. `OCI$RESOURCE_PRINCIPAL` で **Select AI のベクトル索引作成と検索**が通ること
   （SPIKE-M1 の検証スクリプトを流用）。
4. **対照**: 資格情報名を `JETUSE_OCI_CRED`（誤ったプロファイル由来）に戻すと `ORA-20404` になること。
   ＝ 本 ADR の変更が原因を潰したことを示す。
5. 配備済み dev アプリスタックで `/api/health` の `dbchat` が `ok` になること。

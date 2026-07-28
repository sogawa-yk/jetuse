# Public版ワンクリックデプロイ 実機E2E検証（2026-07-28）

「READMEの **Deploy JetUse to Oracle Cloud** ボタンからデプロイしたのにエラーになる」という報告を、
**JetUse開発とは無関係のクリーンなテナンシ**で再現し、原因を潰して再検証した記録。

## 検証環境

| 項目 | 値 |
|---|---|
| テナンシ | DEPLOYTEST（ホームリージョン `ca-toronto-1`／購読 `ap-osaka-1`・`us-chicago-1`・`ca-toronto-1`） |
| 実行者 | テナンシ管理者 |
| デプロイリージョン | `us-chicago-1`（GenAI実証済リージョン） |
| コンパートメント | `jetuse-test`（専用・新規） |
| デプロイ経路 | `orm-main` リリースの `jetuse-orm.zip` を Resource Manager Stack として apply（=ボタンと同一物） |
| ブラウザ検証 | Chromium（Playwright）でログイン〜各機能を実操作 |

修正の検証は、修正後の作業ツリーから同じ手順で作った zip を新しい Stack（`prefix=jetuse2`）へ apply して行った。

## 結論

**現行の公開スタックは、クリーンなテナンシではそのままでは使えなかった。**
Terraform の apply 自体は成功する（176リソース）ため「デプロイは成功したのに動かない」という形で表面化する。
原因は環境固有ではなく、**すべてのユーザーで再現する実装上の欠陥**だった。

修正後、実機E2Eは **38項目中38項目 PASS**（4xx/5xx レスポンスもゼロ）。

## 検出した欠陥と修正

### 1. デモユーザーが初回ログインでパスワード変更を強制され、出力のパスワードで入れない（ブロッカー）

- **症状**: `app_url` を開くと Identity Domain のサインイン画面へ遷移し、`demo_username`/`demo_password`
  を入力すると `/ui/v1/pwdmustchange` に飛ばされる。アプリには到達できない。
  SPAが一瞬描画されてから遷移するため「表示された直後に真っ白になる」（Issue #58）と同じ見え方になる。
- **原因**: Identity Domains は**管理者がSCIMのUserリソースへ password を書くと必ず
  `passwordState.mustChange=true` を立てる**。この属性は readOnly で、PATCH しても作成時に
  `mustChange:false` を渡しても無視される。
- **修正**: ユーザーは password 無しで作成し、`UserPasswordChanger`
  （`PUT /admin/v1/UserPasswordChanger/{id}`）でパスワードを設定する。この経路だと mustChange は false になる。
  Terraform provider に該当リソースが無いため `terraform_data` + `local-exec` で OCI CLI を呼ぶ。
  Resource Manager の実行環境には OCI CLI が委任トークンで認証済みの状態で同梱されており（実機確認）、
  ボタン経由のデプロイで追加作業は要らない（local-exec 実測3秒）。
- **ファイル**: `infra/terraform/modules/identity-domain-app/main.tf`

### 2. Runtime Policy に agentic API の resource-type が無く、既定モデル・RAG回答・会話メモリが全滅（ブロッカー）

- **症状**: チャットが「モデル gpt-oss-120b はこのリージョン/テナンシでは利用できません(HTTP 404)」を返す。
  RAGの質問は「rag requires a responses-family model」で 400。会話メモリは毎回 stateless に縮退。
- **切り分けが難しい理由**: 同じコンパートメント・同じヘッダでも**テナンシ管理者のユーザープリンシパルでは成功**し、
  Container Instance のリソースプリンシパルでのみ 404 になる。`list-models` には gpt-oss-120b が居り、
  `chat/completions` も Vector Store も動くため「リージョン非対応」と誤診しやすい。
- **原因**: `generative-ai-family` に **Responses / Conversations は含まれない**。
  runtime policy に `generative-ai-response` / `generative-ai-conversation` が無かった。
- **確認方法（再利用可）**: policy を1文だけ作ると resource-type の実在を判定できる。
  存在しなければ `CreatePolicy` が 400 `"No permissions found"`、存在すれば（非ホームリージョンでは）
  `"Please go to your home region ... to execute CREATE"` まで進む。
  これで `generative-ai-response` / `-conversation` / `-chat` / `-model` は実在、
  `generative-ai-responses`（複数形）/ `generative-ai-agent-family` は非実在と確認した。
- **修正**: runtime policy に2文追加。付与後 IAM 反映まで実測3〜5分。
- **ファイル**: `infra/terraform/modules/iam/main.tf`

### 3. RAGの初回アップロードが必ずゲートウェイ504になる

- **症状**: UIに「⚠ HTTP 504」。ただしコンテナログではアップロードは 200 で完了している。
- **原因**: 新規デプロイ直後の1件目は Vector Store の CP→DP 伝播待ちを含み実測 **66.7秒**。
  API Gateway の汎用ルート `/api/{p*}` は `read_timeout=60` なので、**ゲートウェイだけがタイムアウト**する。
- **修正**: OCR（`/api/ocr`）と同じく `/api/rag/files` と `/api/rag/{p*}` を `read_timeout=300` の専用ルートにする。
  完全一致ルートの併設は必須（`{p*}` は末尾セグメント無しに一致しない）。
- **ファイル**: `infra/terraform/modules/api-gateway/main.tf`

### 4. 管理ダッシュボードが誰も開けない（常に403）

- **症状**: `/admin` を開くと `GET /api/admin/usage` が 403。
- **原因**: `ADMIN_USERS` をスタックが配線しておらず、`is_admin()` が常に false。
  ワンクリック配備ではログインユーザーが demo しか居ないため、実質誰も開けない。
- **修正**: `ADMIN_USERS` を配線し、既定でスタックが作る `demo` を管理者にする（JWTの `sub` は
  Identity Domain のユーザー名。`email` claim は空）。任意指定用にスタック変数 `admin_users` も追加。
- **ファイル**: `infra/orm/locals.tf` / `variables.tf` / `schema.yaml`

### 5. コンテナ再起動のたびにDBブートストラップが失敗し、25分間「ADB 未準備」と誤表示する

- **症状**: `/api/health` の `dbchat` が恒久的に `unavailable`（`select_ai.ok=null`）。
  実際には NL2SQL は正しく SQL を生成・実行できており、health だけが赤い。
- **原因**: `_ensure_user()` は `CREATE USER` が ORA-01920（既存）なら同じパスワードで `ALTER USER` に落ちるが、
  ADB 26ai の既定プロファイルはパスワード再利用を拒否する（`ORA-28007: The password cannot be reused`）。
  Terraform 生成パスワードは state 固定なので、**イメージ更新・CI再作成・クラッシュ復帰のたびに必ず発生**する。
  `bootstrap()` の外側 `except Exception` がこれを飲み込み「ADB 未準備のため20s後に再試行」を
  25分繰り返して諦めるため、`migrate()` も実行されない。しかも例外を一切ログに出していなかったため、
  プロビジョニング待ちと恒久失敗が見分けられなかった。
- **修正**: ORA-28007 は「既にそのパスワード」として続行する。ただし ORA-28007 は「履歴にある」であって
  「現在値」とは限らないため、実ログインで裏取りしてから進む。リトライログには必ず例外を載せる。
  あわせて bootstrap は `entrypoint.sh` から**別プロセス**で動くため、リソースプリンシパル検証結果を
  ファイル（`RP_STATUS_FILE`、既定 `/tmp/jetuse-rp-status.json`）経由で uvicorn 側へ渡す。
- **ファイル**: `packages/api/jetuse_core/bootstrap.py`

### 6. TTSがPhoenix決め打ちで、Phoenix未購読テナンシでは常に503

- **症状**: `POST /api/tts` が 503「テナンシがus-phoenix-1未購読の可能性」。
- **原因**: `tts_region` の既定が `us-phoenix-1` 固定。実測では **us-chicago-1 で合成成功**、
  `ap-osaka-1` / `ca-toronto-1` は 404 で、提供リージョンは Phoenix 限定ではなくなっている。
- **修正**: 既定を空（= デプロイリージョン → `us-phoenix-1` の順に試行）にし、明示指定時のみ単一リージョンに固定。
  大阪デプロイは従来どおり Phoenix にフォールバックして動く。
- **ファイル**: `packages/api/jetuse_core/tts.py` / `settings.py` / `health.py`

### 7. `/api/health` のTTS判定が偽陽性だった（Codexレビュー F-007 の後追い）

- **症状**: 設定さえ揃っていれば `tts: ok` を返していたため、実際には合成が503で落ちていても health は緑。
- **設計上の落とし穴**: `/api/tts` は API Gateway 経由で **OCI Functions** が処理し、`/api/health` は
  **Container Instance** が返す。したがって「実合成の結果をプロセス内に持って health に反映する」だけでは
  この配備形態では届かない。
- **修正**: 同一プロセスに実合成の結果があればそれを使い、無ければ `list_voices`
  （合成しない=課金なし）で到達性を実測する（結果は5分キャッシュ）。
  なお `list_voices` は `compartment_id` を省くとテナンシルート扱いになり
  **リソースプリンシパルでのみ404**になるため、必ずコンパートメントを渡す。
- **ファイル**: `packages/api/jetuse_core/tts.py` / `health.py`

### 8. 既存Stackのアップグレード経路（Codexレビュー F001 の後追い）

- **問題**: 旧実装で作られた既存 Stack を更新すると、demo ユーザーには**同じパスワードが既に履歴にある**ため
  `UserPasswordChanger` が `pwdpolicyViolation` で拒否し、apply が失敗する。仮に通しても
  `mustChange` が外れずログインできないままになる。
- **修正**: `random_password.demo` に `keepers` を入れ、**更新時に必ず新しいパスワードを発行**する
  （出力 `demo_password` も新しい値になる）。あわせて local-exec は 2回目以降の
  `pwdpolicyViolation` を「直前のPUTは成功して応答だけ失われた」ケースとして成功扱いにし、
  再実行可能にする。
- **実機確認**: 旧実装で作った Stack の config-source だけを修正版に差し替えて apply → 成功、
  パスワードがローテートされ、新しい値でパスワード変更を強制されずログインでき、
  `GET /api/me` が `is_admin: true` を返すことを確認（`runs/.../e2e/upgrade-path.md`）。
- **ファイル**: `infra/orm/main.tf` / `infra/terraform/modules/identity-domain-app/main.tf`

### 9. Stack を destroy できない（アプリを一度でも使うと teardown が必ず失敗）

- **症状**: `terraform destroy`（Resource Manager の Destroy）が2箇所で失敗する。
  1. `409-BucketNotEmpty, Bucket named '<prefix>-app-data' is not empty.`
  2. `Error running command 'oci identity-domains app patch --force ...' / No such option: --force`
- **原因**:
  1. OCI provider に `force_destroy` 相当が無く、Terraform は**自分が作ったオブジェクトしか消さない**。
     RAGアップロード・議事録音声などアプリが実行時に書いたオブジェクトが残るため、
     「アプリを使ったユーザーはスタックを消せない」状態になる。
  2. OIDCアプリを destroy 前に非アクティブ化する provisioner が使う `oci identity-domains app patch`
     には `--force` が**存在しない**（`user-password-changer put` 等にはある）。CLIは複合型の置換で
     y/N を尋ねるため、非対話環境では `y` を流し込む必要がある。
- **修正**: バケット削除の直前に `oci os object bulk-delete` / `bulk-delete-versions` /
  `multipart abort` を走らせる destroy-time provisioner を追加（`for_each` のキーは apply 前に
  確定する必要があるため静的名 + `depends_on` で順序担保）。`app patch` は `echo y |` に変更。
- **実機確認**: 修正前の destroy が上記2件で FAILED になることを確認したうえで、修正版の設定で
  再実行して **destroy SUCCEEDED**（`runs/.../e2e/teardown.txt`）。
- **ファイル**: `infra/terraform/modules/object-storage/main.tf` /
  `infra/terraform/modules/identity-domain-app/main.tf`

### 10. 付随修正

- `oci identity-domains ... patch` は非対話環境で確認プロンプトを出すため `--force` が要る。
  destroy時にOIDCアプリを非アクティブ化する provisioner が teardown で失敗しうるので追加した。
- `scripts/package-orm-stacks.sh` の `sed -i` が BSD sed（macOS）で壊れており、開発者がローカルで
  配布zipを検証できなかった。一時ファイル経由の移植可能な形へ修正。
- `admin_users` は空白のみの入力で「管理者0人」に落ちないよう `trimspace` して判定・送出する。
- TTSがPhoenix限定である前提の記述（`docs/guides/customize.md` / `docs/KNOWLEDGE.md` /
  `docs/guides/HANDOVER.md` / `CLAUDE.md`）を自動選択仕様へ更新した。

## E2E結果（修正後）

**38項目すべて PASS**（4xx/5xx レスポンスもゼロ）。最終コードから作った ORM zip とコンテナイメージで
**まっさらな新規スタックを作成 → E2E → destroy** まで通している（destroy も
`Destroy complete! Resources: 180 destroyed.` で成功）。実行ログと合否一覧は
`runs/2026-07-28T2226_PUBLIC-DEPLOY-E2E/e2e/e2e-results.json`、後始末は `teardown.txt`。

| 結果 | 項目 |
|---|---|
| PASS | デモユーザーがパスワード変更を強制されない |
| PASS | ログイン→アプリ表示 |
| PASS | /api/health 全体が ok |
| PASS | capability: chat |
| PASS | capability: rag |
| PASS | capability: dbchat |
| PASS | capability: speech |
| PASS | capability: ocr |
| PASS | capability: tts |
| PASS | /api/rag/health 3点すべて ok |
| PASS | チャット: 既定モデル(gpt-oss-120b)が応答 |
| PASS | チャット: 既定モデルのフォールバック通知が出ない |
| PASS | チャット: モデル llama-3.3-70b |
| PASS | チャット: モデル gemini-2.5-pro |
| PASS | チャット: モデル gemini-2.5-flash |
| PASS | チャット: モデル llama-3.2-90b-vision |
| PASS | 会話メモリ: 会話作成(OCI Conversations) |
| PASS | RAG: 初回アップロードがゲートウェイ504にならない |
| PASS | RAG: 文書取り込み完了 |
| PASS | RAG: Vector Store 索引化完了 |
| PASS | RAG: 文書に基づく回答(引用付き) |
| PASS | DBチャット: SQL生成 |
| PASS | DBチャット: SQL実行と結果表示 |
| PASS | OCR: 画像から文字抽出 |
| PASS | 議事録: 音声登録がエラーにならない |
| PASS | リアルタイムSTT: セッション作成 |
| PASS | TTS: 音声合成 |
| PASS | TTS: health が実合成の結果を反映(verified) |
| PASS | 翻訳 |
| PASS | エージェント一覧 |
| PASS | 管理ダッシュボード: demoユーザーが閲覧できる |
| PASS | ページ描画: ホーム |
| PASS | ページ描画: チャット |
| PASS | ページ描画: リアルタイム翻訳 |
| PASS | ページ描画: 音声チャット |
| PASS | ページ描画: 映像分析 |
| PASS | ページ描画: ビルダー |
| PASS | ページ描画: 設定 |

`/api/health` は `ok: true`（chat / rag / dbchat / speech / ocr / tts がすべて ok）。
`dbchat` の `semantic_store` は `SEMSTORE_OCID` 未設定のため false だが、Select AI 経路が
生きているため capability としては ok（NL2SQL の生成・実行も実機で成功）。


## 検証の範囲（どこまでを実機で確認したか）

- **実機で通したもの**: 新規デプロイ（apply）→ ブラウザE2E 38/38 → destroy の一巡を、最終コードから
  作った ORM zip とコンテナイメージで実施。加えて、旧実装で作った Stack の**アップグレード経路**と、
  修正前に必ず失敗していた **destroy** の解消も実機で確認した。
- **実機で通していないもの**: `runs/2026-07-28T2226_PUBLIC-DEPLOY-E2E/e2e/SKIPPED.md` に理由・代替検証・
  残存リスクを列挙（TTSの大阪→Phoenixフォールバック / デモスコープ経路 / IAM分割パターン /
  UserPasswordChanger の応答喪失経路）。
- **最終E2E後に入れた変更**: destroy 用 local-exec への `OCI_CLI_REGION` 明示、
  `infra/terraform/environments/dev` 側への同じ destroy 順序の反映、ドキュメント更新のみ。
  アプリの実行時挙動には影響しない（実行経路のコードは E2E 実施時と同一）。

## 環境依存として切り分けたもの（コード修正の対象外）

- **GenAI検証済リージョン**: 本検証は `us-chicago-1`。`ap-osaka-1` は既存の実績どおり。
  `ap-tokyo-1` / `us-ashburn-1` は従来どおり `allow_unvalidated_genai_region` の明示オプトインが必要。
- **SQL Search（Semantic Store）**: `semstore_ocid` は利用者が事前作成したストアを指す任意設定で、
  未設定時は DBチャットが Select AI 経路へ自動切替される（本検証もその経路で正答）。

## 再現手順

```bash
# 1. 配布zip（= ボタンの中身）をローカルで作る
npm --prefix packages/web ci && npm --prefix packages/web run build
bash scripts/package-orm-stacks.sh dist/orm

# 2. Stack を作って apply（--variables に compartment_ocid / tenancy_ocid / region）
oci resource-manager stack create --config-source dist/orm/jetuse-orm.zip ...
oci resource-manager job create-apply-job --execution-plan-strategy AUTO_APPROVED ...

# 3. 出力の app_url / demo_username / demo_password でブラウザE2E
```

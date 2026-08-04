# TOOL-01 検証レポート — デモ固有の HTTP ツールをエージェントに渡す口

- 日付: 2026-08-01
- run: `runs/2026-08-01T2002_TOOL-01/`
- 実行環境: 共有 loop ADB(`jetuse-loop-adb`)の run 固有スキーマ `JETUSE_TOOL01_68D006` /
  dev コンパートメント。架空の業務 API(Object Storage + PAR)・Vault・
  OCI Generative AI(`gpt-oss-120b`)はすべて実物。

## 何ができるようになったか

デモ側が持つ**素の HTTP エンドポイント**を、名前・説明・JSON Schema つきで JetUse に登録し、
エージェント実行時に組込ツールと同列に配線できるようになった。モデルがそれを呼ぶと
**JetUse がサーバー側で HTTP を代理実行**して結果を返す。ブラウザからは叩かせない。

```
POST /api/agent/http-tools        登録(name / description / parameters / url / method / 認証)
GET  /api/agent/http-tools        所有者のツール一覧(秘密の OCID は返さない)
DELETE /api/agent/http-tools/{id} 削除(所有者のみ。0 行 = 404)
POST /api/chat/stream             agent=true + http_tool_ids=[...] で実行に配線
POST /api/agent/execute-tool      承認後の単発実行(外部ツールは http_tool_id 必須)
```

MCP サーバー登録(`/api/agent/mcp-servers`)は**作り替えていない**。別経路として共存する
(MCP は OCI 側でサーバーサイド実行、こちらは JetUse が自分で HTTP を叩く)。

## 完了条件に対する結果

| 完了条件 | 結果 | 根拠 |
|---|---|---|
| 外部 HTTP ツールを登録 → モデルが呼ぶ → 結果が回答に反映 | PASS | `e2e/scenario-1.md` |
| 秘密は Vault 参照で渡り DB にも API 応答にも平文で出ない | PASS | `e2e/scenario-2.md` / `test_secret_never_in_db_columns_or_api_response` |
| 内部メタデータ・ループバック等の登録が拒否される | PASS | `e2e/scenario-3.md`(7 種すべて 400・1 件も保存されず) |
| タイムアウト・サイズ上限超過が黙って切り詰められない | PASS | `e2e/scenario-4.md` / `e2e/scenario-5.md` |
| 既存の組込ツール・MCP 経路が回帰しない | PASS | `pytest packages/api/tests` 688 件パス(`e2e/checks.md`)(MCP・エージェントの既存テスト含む) |
| lint / test | PASS | `ruff check packages/api spikes ops` クリーン(`e2e/checks.md`)|

## 設計上の判断(なぜこの形か)

### 1. `ToolDef` に寄せて「そのターン限りのレジストリ」を渡す

外部ツールは所有者ごとの資源なので、`tools.TOOLS`(プロセス共有の辞書)には入れられない。
そこで `tools.execute_tool` を `execute_with(registry, name, args)` に分解し、
`chat.stream_agent` が `{外部ツール, **TOOLS}` を組み立てて 1 ターンだけ使う。

```python
registry = {**{t.name: t for t in (http_tools or [])}, **TOOLS}
```

**組込を後勝ちにしている**のは、万一同名が登録されていても組込ツールが乗っ取られないため
(登録時にも予約名として拒否しているので二重の守り)。承認要否・承認待ち通知・実行のすべてが
このレジストリを見るので、組込と外部の扱いは同列になる。

### 2. 秘密は既存の Vault 流儀に完全に揃えた + **どの秘密を使えるかを認可した**

`mcp_servers` が持つ `auth_secret_ocid` と同じ列名・同じ読み出し関数(`_read_secret`)を使う。
新方式は作っていない。登録 API は**秘密そのものを受け取らない**(OCID だけ)。
DB のスキーマにも平文の秘密列は無い(`e2e/deploy.log` に実際の列定義を残した)。

`mcp_servers` との唯一の違いは**ヘッダ名を選べる**こと(既定 `Authorization`)。素の業務 API は
`X-Api-Key` のような独自ヘッダを要求することが多く、`Bearer` 固定では繋がらないため。
秘密の置き場・参照方法・読み出し経路は変えていない。

**ここに一つ穴があった**(Codex レビュー review-1 の blocker)。OCID を利用者が自由に書ける
ままだと、「サービスの OCI 権限で読める**任意の**秘密を、利用者が指定した外部 URL へ送らせる」
経路(confused deputy)になる。ADB の管理者パスワードでも運用用のトークンでも、OCID さえ
分かれば自分のエンドポイントへ流出させられる。

対策は**登録時に Vault のメタデータ(値ではない)を引いて認可する**こと:

- 秘密が**本アプリのコンパートメント**にあること
- 秘密の **freeform タグ `jetuse_tool_owner` が登録者(`AuthContext.subject`)と一致**すること

参照できない・タグが無い・他人のタグ・別コンパートメント・**`COMPARTMENT_OCID` 未設定**は、
すべて 400 で断る(fail-closed。照合先が無いまま素通しにすると認可境界そのものが消える)。
この認可は**実行のたびに取り直す**ので、登録後に Vault のタグを外せばその場で使えなくなる。
デモ担当者は「自分用の秘密を作ってタグを付ける」だけでよく、新しい秘密管理方式は要らない。
実環境で、運用用の既存 Secret を指定した登録が拒否されることを確かめた(`e2e/scenario-2.md`)。

**応答に反射された秘密も伏せる**(review-2 の blocker)。エコー系・デバッグ用エンドポイントは
受け取った認証ヘッダを応答本文に含めることがある。それを素通しすると、秘密がモデル・UI の
ツール結果プレビュー・会話履歴へ流れる。そこで **送った秘密を応答本文から `<redacted>` へ
置換**してから返す(エラー本文も同様)。反射の形は生とは限らないので、生 / JSON エスケープ /
URL エンコード / Base64 / hex の各表現を潰す(review-3 の指摘)。

これは**完全な保証ではない**。敵対的な相手は秘密を任意に変形して返せる(分割・ハッシュ・
文字コード変換)ので、変形をすべて追うことは原理的にできない。本当の制御は「どの URL に
どの秘密を渡すかを登録者が決める」ことであり、伏せ字は**善意の相手が誤って反射した場合**を
確実に潰すための層である。scenario-2 はこの伏せ字が `x-api-key` の位置に現れることをもって
「正しく届いた」と「平文が出ない」を同時に示している。

> `mcp_servers` は認証付き登録を 501 で塞いでいるため、この認可が要る経路は本タスクが最初。
> 将来 MCP 側の認証を開けるときは同じ `assert_secret_usable` を通すこと。

### 3. SSRF は登録時と実行時の両方で fail-closed

URL 検証は `mcp_servers.validate_url` と同じ経路(`jetuse_shared.webtools.assert_public_host`)。
https 必須で、名前解決の結果が private / loopback / link-local(169.254.x)/ reserved /
multicast / unspecified なら拒否する。加えて本タスクで足したのは:

- **URL に認証情報を書けない**(`https://user:pass@…` を拒否。秘密は Vault へ)
- **宛先や本文の枠組みを決めるヘッダを認証ヘッダにできない**(`Host` / `Content-Length` /
  `Transfer-Encoding` 等)。`Host` を握られると IP ピン留めが固定した origin を動かされる。
  実行時にも Host を検証済みホストで上書きする(二重の守り)
- **ポートを検証する**(数値でない・0・範囲外は登録時に拒否。実行時の ValueError と
  「0 が 443 に化ける」を防ぐ)
- **実行時にも同じ検証を通す**(登録後に DNS が内部を向いても止まる)
- **接続先の IP をピン留めする**(下記)
- **3xx はすべて失敗**。Location の有無で判定しない(`is_redirect` は Location 付きしか
  真にならず、300 / 304 / 307 等が素通りする)
- **メソッドは GET / POST のみ**。引数は GET ならクエリ、POST なら JSON ボディに載る。
  URL のホスト・パスは引数で動かせない

**DNS リバインディングも塞いだ**(review-3 の blocker)。「検証のときは公開 IP、接続のときは
169.254.169.254」を返す DNS を攻撃者が用意すると、ホスト名だけを検証する実装は破られる。
そこで `_pin_target()` が

1. 名前解決を **1 回だけ**行い、
2. 返ってきたアドレスを**すべて**共有の判定規則(`assert_public_host`)に通し、
3. **その IP を URL に埋めて接続**する(Host ヘッダと TLS の SNI は元のホスト名のまま)

という形にした。証明書検証は本来のホスト名に対して行われる(実 Object Storage /
postman-echo で通ることを E2E で確認済み)。判定規則そのものは `jetuse_shared` の
関数をそのまま使っており、独自の許可・拒否リストは作っていない。

> `web_fetch` / MCP 側は従来のまま(ホスト名検証のみ)。**この経路だけが強い**状態なので、
> 共通ライブラリ側へ引き上げるかは別タスクの判断(`e2e/SKIPPED.md` §4)。

### 4. 上限は「切り詰めずに失敗させる」

| 既定 | 値 | 理由 |
|---|---|---|
| タイムアウト | 15 秒 | 相手が返らないときにエージェントのホップを止める |
| 応答サイズ | 128,000 バイト | 超過は失敗。途中まで読んだ本文を「結果」としてモデルへ渡さない |
| 1 回の読み出し | 16,384 バイト | 上限判定の粒度。**足す前に**測るので 1 チャンク分も越えて持たない |
| リトライ | 0 回 | 業務 API の二重実行(発注・更新)を避ける |
| 1 エージェントあたりのツール数 | 8 | 多すぎるとモデルの選択精度が落ちる。組込 4 種と足しても十数件 |

切り詰めた本文をそれらしく返すと、モデルは「取得できた」と誤解して誤答する。だから
`HttpToolCallError`(= `tools.ToolError` の派生)を投げ、モデルには
「ツール実行に失敗しました: …」として届く。

サイズ上限は「小さく送って大きく展開させる」応答(圧縮爆弾)ですり抜けられてはいけないので、
(a) `Accept-Encoding: identity` で圧縮を要求せず、(b) `Content-Length` の申告が上限を超えて
いれば 1 バイトも読まず、(c) 読む場合も**足す前に**測る、の 3 段にした(review-5)。
実 Object Storage に `Content-Encoding: gzip` で置いた「展開後 5MB / 送信は数 KB」の
オブジェクトで実測している(`e2e/scenario-6.md`)。

### 5. 引数スキーマは「検証しきれる形」だけ通す

`parameters` はスカラ型(string / number / integer / boolean)最大 20 個の平坦な object のみ。
`tools._validate_args` が実行前に必須・未知キー・型を検査するので、ネストや配列を許すと
検証を素通りする引数ができてしまう。表現力より fail-closed を採った。

同じ理由で、**検証できない JSON Schema キーワード(`enum` / `pattern` 等)は保存時に落とす**
(残すのは `type` と `description` だけ)。モデルには制約に見えるのに実行前検証は素通し、
という食い違いを作らないため。`integer` は小数を弾く(`1.5` は通さない)。

### 6. 失敗の伝え方を混ぜない

- 解決できないツール id は **404**(下記)
- 承認後の実行で id が解決できなければ **404**(削除済み・他人所有・不正 id)
- 承認後の実行は **`http_tool_id` 必須**。名前だけの再解決は 400 で断る(名前で引き直すと、
  承認待ちの間に削除 → 同名で別 URL・別 Secret のツールを作られたときに、利用者が確認したのと
  違う HTTP 操作が走る)。id が指す行の名前が変わっていれば **409**
- 代理実行の失敗(SSRF 再検証・Vault 読み出し・タイムアウト・サイズ超過・非 2xx)は
  すべて `HttpToolCallError` に揃える → ルートでは **400**、エージェント実行中はモデルへ
  「ツール実行に失敗しました…」として届く
- レジストリ照会そのものの失敗(DB 障害等)は**握り潰さない**。存在しないツールと同じ 400 に
  すると、呼び出し側も監視もサービス障害を区別できない(共通ハンドラが **503**)

### 7. 解決できないツール id は 404 で止める

`http_tool_ids` に削除済み・他人所有・不正な id が混じっていたら、取れた分だけで実行を続けず
404 にする。黙って外すと「業務 API を参照しないまま、もっともらしい回答を返す」ことになる
(所有者強制の 0 行 = 404 と同じ扱い)。

## 実環境 E2E の証跡

| # | 確かめたこと | 結果 | 証跡 |
|---|---|---|---|
| 1 | 架空の在庫 API をツール登録 → エージェントが自分で呼び、在庫数 137 / 大阪第2倉庫 / LOT-2026-0731 が回答に載った(組込ツールは 1 つも渡していない) | PASS | `e2e/scenario-1.md` |
| 2 | Vault の秘密が `X-Api-Key` として相手に届き(反射された値が伏せ字になることで確認)、応答本文・DB・API 応答のいずれにも平文が出ない。認証なしツールでは付かない。**運用用の別 Secret の OCID を指定した登録は 400 で拒否**された | PASS | `e2e/scenario-2.md` |
| 3 | 否定: メタデータ 2 形式・ループバック・localhost・私有レンジ・平文 http・URL 埋め込み認証情報の 7 種すべて 400 で、1 件も保存されない | PASS | `e2e/scenario-3.md` |
| 4 | 5 秒かかる実 API に対し上限 2 秒で「タイムアウト」として失敗 | PASS | `e2e/scenario-4.md` |
| 5 | 300,000 バイトの実オブジェクトが上限超過で失敗(切り詰めなし) | PASS | `e2e/scenario-5.md` |
| 6 | `Content-Encoding: gzip` で展開後 5MB になる実オブジェクトも上限で失敗(圧縮爆弾) | PASS | `e2e/scenario-6.md` |

証跡には Vault の秘密の平文・PAR トークン・OCID の実値を残していない(伏せ字と先頭のみ)。
実施しなかった範囲と理由は `e2e/SKIPPED.md`。

## 再現手順

```bash
E="SPIKE_SCHEMA_PREFIX=JETUSE_TOOL01 SPIKE_HOME=/tmp/jetuse-tool01"
P="PYTHONPATH=spikes/ragm02:spikes/tool01:packages/api"
# 検証用の Vault 秘密は人が一度だけ作る(jetuse-spike-tool01-apikey)。
# freeform タグ jetuse_tool_owner=dev-user を付けること(付いていないと登録が 400 になる)
env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # run 固有スキーマ
env $E $P .venv/bin/python spikes/tool01/deploy.py         # マイグレーション適用
env $E $P .venv/bin/python spikes/tool01/e2e.py            # 6 シナリオ
env $E $P .venv/bin/python spikes/tool01/teardown.py --yes # OCI 側(バケット・PAR)の片付け
env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes # ADB スキーマ
```

Vault 秘密は teardown が消さない(固定名で所有を証明できないため。不要なら人が削除予約する)。

`ADB_COMPARTMENT_OCID` は `.env` の `COMPARTMENT_OCID` と同じ値を渡す。
共通部の `resolve_dev_compartment()` は「承認済みの根の**直下**の `dev`」を探すが、
現在の `.env` の `COMPARTMENT_OCID` は既に `dev` そのものを指しており子が無いため
(2026-08-01 実測)、明示指定が要る。

## 未検証・今後

- **DNS リバインディング対策は本経路だけ**。`_pin_target`(検証した IP へ接続 + Host/SNI 維持)を
  入れたのはこの外部 HTTP ツールのみで、既存の `web_fetch` / MCP はホスト名検証のまま。
  共通ライブラリ(`jetuse_shared`)へ引き上げて全経路を揃えるかは別タスクの判断。
  攻撃側(1 回目は公開 IP・2 回目は内部 IP を返す権威 DNS)の実演は共有環境では組めない
  (`e2e/SKIPPED.md` §4。単体テストで解決結果の混入と Host/SNI の維持を固定してある)。
- **画面は無い**。デモ側は API を直接叩いて登録する。
- **能力カタログ**(`/api/capabilities` の `agents`)には実測できた範囲だけを書いた
  (メソッド GET/POST・上限値・URL ポリシー・秘密の扱い)。未実証の項目は書いていない。

## 受容した residual（2026-08-01 人間ゲート）

**呼び出し先 URL は平文で保存し、API 応答にも返す。** Codex の指摘どおり、PAR や署名付き URL は
**パス・クエリ自体が資格情報**なので「秘密は Vault だけ」という建前と食い違う。

- **判断**: このまま（対処を入れない）。デモ用途では URL を隠すと登録内容の確認・切り分けが
  できなくなり、実害（登録者本人しか見られない）に対して代償が大きい。
- **代わりにやったこと**: `specs/17` に「**そういう URL を登録しないこと**」を明記し、
  `docs/tips.md` に一般化した教訓（外部の宛先を登録させる機能では URL も秘密になりうる）を残した。
- **将来の再検討条件**: PAR を扱う要求が出たら、URL のマスクか PAR 形式の登録拒否のどちらかを入れる。

## 検証用資源の片付け（2026-08-01 実施）

| 対象 | 結果 |
|---|---|
| バケット・オブジェクト・PAR | 削除済み |
| ADB の run 固有スキーマ | `DROP USER ... CASCADE` 済。再照会でユーザー 0 / ACL 0 |
| ローカルの認証資材 | 削除済み |
| Vault 秘密 `jetuse-spike-tool01-apikey` | **予約削除**（`SCHEDULING_DELETION` / 2026-08-09）。Vault は即時削除できず 7 日以上先の予約が必要 |

補足（解決済み）: 片付け時に `spikes/ragm02/common.py:resolve_dev_compartment()` が
「`COMPARTMENT_OCID` は**親**で、その直下の `dev` を探す」前提だったため、`.env` を
`dev` 自身にしていると「一意に定まらない」で中止した（実際に踏んだ）。

**2026-08-01 に施主が規則を明言**: dev ブランチ派生の作業では
**`COMPARTMENT_OCID` = `jetuse:dev` コンパートメントそのもの**。
`ops/_adb.assert_target()` も「`COMPARTMENT_OCID` そのもの」を要求しており、そちらが正だった。
`resolve_dev_compartment()` を**両対応**（自身が `dev` ならそれを使い、そうでなければ従来どおり
直下を探す）に修正した。いずれも**名前が `dev` であること**を要求する＝fail-closed は維持。

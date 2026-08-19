# 実環境検証の範囲（前提チェック `make doctor`）

**実 OCI の資源は作らない。** この差分は「環境が揃っているか」を確かめる道具で、
資源を触らない。確かめるべきは2方向 —— **揃っていれば通り、欠けていれば止まるか**。

| | 内容 | 結果 | 記録 |
|---|---|---|---|
| 1 | 揃っている環境（この手元） | 通る（exit 0） | `01_doctor_ok.txt` |
| 2 | `codex` を隠す | **NG 1件で非ゼロ**。他は OK のまま（原因が切り分けられている） | `02_doctor_missing_codex.txt` |
| 3 | `.env` が雛形のまま | **非ゼロ**。`COMPARTMENT_OCID` 等を名指しする | `03_doctor_template_env.txt` |
| 4 | 変異検査 | A: `kubectl` を**4つの書き方**（素・`if !`・`VAR=x`・パイプ後）で足して全部検出 / B: doctor が常に失敗する形にすると FAIL | `04_mutation.txt` |
| 5 | **`.env` を退避**して CI 相当にする | テストは通り、doctor 自体は正しく落ちる | `05_isolation.txt` |
| 6 | `AUTH_MODE` の5パターン | typo は NG、引用付きは OK、principal 系は「不要」 | `06_auth_mode.txt` |
| 7 | **CI で skip しないこと** | SKIPPED 0 件 | `07_no_skip.txt` |
| 8 | `~/.oci/config` の中身 | 空の `[DEFAULT]` も、`key_file` が指す先が無いものも NG | `08_oci_profile.txt` |

**実行環境は macOS の手元**（bash 3.2 / BSD date / Python 3.14 / AUTH_MODE=config_file）。
`dev` インスタンス（Python 3.12 / instance_principal）では実行していない。
この差分は資源を触らないため実機差の影響は小さいが、**dev ホストでの実行は未確認**。

## なぜ変異検査を載せるか

**最初に書いたテストは、どちらの変異も素通りした。**

- `_CMD` の正規表現に `re.M` が無く、**行頭のコマンドを1つも見ていなかった**
  （`kubectl` を足しても検出しない）
- 「欠けたら落ちる」テストしか無く、**常に落ちる doctor** と区別できなかった

前者は `re.M` を足し、後者は「揃っていれば通る」テストを足して塞いだ。
**テストがあることと、テストが効いていることは別**。

## doctor が陳腐化しない仕組み

**`ops/*.sh`** が実際に呼ぶ外部コマンドを機械で拾い、
doctor が見ている集合と照合する（`test_every_external_command_used_by_ops_is_checked`）。
新しいコマンドを使い始めて doctor に足し忘れると CI が落ちる。

検出の精度を上げるために、以下はコマンドとして数えない（どれも実際に誤検出した）。

| 落とすもの | 例 |
|---|---|
| シェルの構文・組み込み | `do` / `fi` / `until` / `trap` |
| `ops/*.sh` のどこかで定義された関数 | `jetuse_ocir_host`（`_region.sh` で定義・別ファイルから呼ぶ） |
| `case` の分岐ラベル行 | `plan\|apply\|import\|state)` |
| クォートの中身 | `grep -E "will be destroyed\|must be replaced"` の `\|` |
| heredoc の本文 | 案内文の「base を public-dev に…」 |
| 埋め込み Python | `python3 -c '...'` の `print` / `raise` |
| 変数代入・行末コメント | `rc=1` / `hard() {  # name, found(0/1)` |
| 算術展開 | `$((found + 1))` を `$(` で割ると `found` が先頭語に見える |

**行ごとに処理する。** `'[^']*'` をファイル全体に当てると、離れた行のアポストロフィと
対になって広い範囲を巻き込み、構造が壊れる（誤検出の主因だった）。

**走査対象は `ops/` だけ。** `.claude/skills/*/scripts/` も見にいったが、レビュー指示の
長い散文から `blocker` / `major` を拾ってしまい精度が保てなかった。そこから来る唯一の依存
`codex` は、名指しのテストで押さえている。

## レビューで直した点

| 指摘 | 対応 |
|---|---|
| `AUTH_MODE` を無視して `~/.oci/config` を必須にしていた | `instance_principal` / `resource_principal` では不要と判定する。**正しい環境を落としていた** |
| `.env` は存在するだけで OK だった | 雛形のまま（値が空・プレースホルダ）を検出する。ただし**要求するのはスクリプトが必須と宣言している鍵だけ**（`AUTH_MODE` 等は既定があり空で正常） |
| テストの shim が偽物だった | `command -v` はシェル組み込みで `subprocess` から呼べず、全部が「exit 0 するだけ」になっていた。`shutil.which` で実体を引く |
| ツールが揃わない環境で検査が skip されていた | **偽物を置いて必ず走らせる**。版を見る項目（terraform 1.7+ / node 22+）は通る値を返させる。skip では doctor が壊れていても気づけない |
| `~/.oci/config` はセクション名しか見ていなかった | 空の `[DEFAULT]` や鍵の欠けたプロファイルを通していた。`tenancy` / `user` / `fingerprint` / `key_file` の有無まで見る（`security_token_file` があるセッション認証は除く） |
| 「`.claude/skills` も照合する」と書いたが実装は `ops/` のみだった | **私が書いた説明が事実と違っていた**。ドキュメントを実装に合わせて訂正 |
| 証跡が古い出力のままだった | `oci config 3 プロファイル` は改修前の表示。**最終形で取り直した** |
| セッション認証で `key_file` を不要にしていた | 署名鍵はどちらの認証でも要る。不要なのは `user` / `fingerprint` だけ |
| Python の版判定が文字列パターンだった | `3.1[23]|3.1[4-9]` は将来の 3.20 を弾く。数値比較にした |
| 版が足りないホストで正常系が落ちた | Terraform 1.6 / Node 20 の環境では実物でなく固定の偽物を使う（doctor の欠陥と区別が付かなくなるため） |
| テストが実リポジトリの `.env` / `~/.oci/config` に依存していた | **クリーンな checkout（CI）で必ず落ちる**。テスト側で `ops/` を symlink した一時リポジトリと合成 `.env` / `HOME` を作る |
| `config_file` 以外の `AUTH_MODE` を一律「不要」にしていた | typo や `AUTH_MODE="config_file"` が素通りする。既知の3値だけ許し、他は NG |

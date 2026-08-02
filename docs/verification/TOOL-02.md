# TOOL-02 検証レポート — 外部 HTTP ツールの固定ヘッダと冪等キー発行

- 日付: 2026-08-02
- run: `runs/2026-08-02T1107_TOOL-02/`
- 実行環境: 共有 loop ADB(`jetuse-loop-adb`)の run 固有スキーマ `JETUSE_TOOL02_6BB0A7` /
  dev コンパートメント。ADB・Vault・OCI Generative AI(`gpt-oss-120b`)・相手の https
  エンドポイントはすべて実物。
- 決定: `docs/decisions/ADR-0023-http-tool-extra-headers.md`(**Proposed。§4 は人間レビュー必須**)

## 実環境で見つかった経緯(なぜこのタスクが必要になったか)

TOOL-01 は外部 HTTP ツールの認証を `auth_header` + `auth_secret_ocid`(Vault 参照)の
**1 組だけ**送る設計にした。実案件のデモで、**認証キー / 追跡 ID / 冪等キーの 3 つを
必須とする API** に当たり、そこで初めて次が分かった。

| 事実 | 内容 |
|---|---|
| 手動での疎通 | 3 つ揃えれば 200(相手の API は正しく動作している) |
| 不足時の応答 | `400 MISSING_CORRELATION_ID` / `400 MISSING_IDEMPOTENCY_KEY` |
| JetUse 経由 | **登録すら意味を成さない**(必ず 400) |

**回避策(ゲートウェイで固定値を注入する)を採らなかった理由**: 冪等キーは呼び出しごとに
変わる前提の仕組みで、固定すると同一パスへ異なるボディで 2 回目を呼んだ時点で
`409 IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INPUT` になる。このデモの測定対象は
「エージェントがエラーから自己修正して呼び直せるか」であり、そこが機構の都合で機械的に
拒否されると**エージェントの能力ではなく仕掛けの制約で失敗する**＝測定値が意味を失う。
そこで本体側で解決した(2026-08-02 人間ゲートで承認済み)。

## 何ができるようになったか

```
POST /api/agent/http-tools
  headers:            {"X-Correlation-Id": "...", "X-Client-Version": "..."}   # 毎回付く固定値
  idempotency_header: "X-Idempotency-Key"                                      # 名前だけ登録
```

- 固定ヘッダは最大 5 個(`MAX_EXTRA_HEADERS`)・値は印字可能 ASCII 200 文字まで
  (`MAX_HEADER_VALUE_LENGTH`)。ヘッダ名は RFC 9110 の token(`X_Trace` や `api.version`
  のような名前を要求する相手がいるため、認証ヘッダ名より広い。区切り文字・制御文字は不可)。
- **冪等キーの値は登録しない。呼び出しのたびに JetUse が uuid4 を発行して送る**
  (モデルには作らせない — 使い回されると二重実行防止が効かなくなる。ADR-0023 §2)。
- ヘッダの組み立て順は **固定 → 冪等 → 認証 → Host**。後から入るものが必ず勝つので、
  固定ヘッダで認証や宛先を上書きできない。
- 検証は**登録時と実行時の両方**。登録時だけだと、後から DB を直接触られた場合に素通りする。
- 既存ツールは `extra_headers` / `idempotency_header` とも NULL で、**送るヘッダが 1 つも
  増えない**(挙動不変)。

## 完了条件に対する結果

| 完了条件 | 結果 | 根拠 |
|---|---|---|
| 固定ヘッダを付けたツールが登録でき、実行時にその値が相手へ届く | PASS | `e2e/scenario-1.md`(相手の受信ヘッダ) |
| `idempotency_header` を 2 回呼ぶと異なる値が送られる | PASS | `e2e/scenario-2.md` / `test_idempotency_key_is_new_on_every_call` |
| 固定ヘッダで `Host` / 認証ヘッダを上書きできない | PASS | 実環境は**拒否**まで(`e2e/scenario-4.md`)。順序の担保は `test_legit_values_win_over_fixed_headers`(理由 `e2e/SKIPPED.md` §2) |
| 禁止ヘッダ・CR/LF・上限超過が登録時にも実行時にも拒否される | PASS | `e2e/scenario-4.md`(実 ADB の行を直接 UPDATE してから実行) |
| 既存ツール(両方 NULL)の挙動がまったく変わらない | PASS | `e2e/scenario-3.md`(DB 上も NULL・増えたヘッダなし) |
| `pytest packages/api/tests` / `ruff check` | PASS | 749 件パス・クリーン(`e2e/checks.md`) |

## 設計上の判断(なぜこの形か)

### 1. 冪等キーは JetUse が発行する(モデルに作らせない)

番号を作るのは「呼ぶ側」の責任であり、実際に HTTP を出しているのは JetUse である。
モデルに任せるとリトライや会話再開で**同じ値を使い回す**——二重実行防止が効かなくなり、
しかもモデルの気まぐれで壊れ方が変わる。ここは決定論に倒した(ADR-0023 §2)。

これは一般のテンプレート機構ではない。`{{...}}` のような動的置換は**作っていない**。
特別扱いするのは冪等キーだけ。

### 2. 順序と検証の二重の守り

禁止ヘッダの検証をすり抜けても、認証と Host は**後から**入るので必ず正規の値が勝つ。
逆に、順序があるからといって検証を省くと「相手に見えるヘッダを利用者が自由に足せる」
状態が残る(`Cookie` や `Authorization` を平文で持ち込める)。両方を置いた。

### 3. 壊れた DB 値は「障害」ではなく「拒否」にする(review-2 の指摘)

`extra_headers` は CLOB なので、直接書き換えられれば JSON として壊れた値も入りうる。
そこを素直に `json.loads` すると、**壊れた 1 行で一覧 API 全体が 500 になる**
(拒否したいものが障害になる)。読み出し側で上限長・parse 失敗を dict でない印に変え、
実行時検証で必ず弾く形にした。一覧では `headers_invalid: true` として**隠さず**返す。
実環境で壊れた CLOB(`not-json` / JSON 配列)を実 ADB へ書き込み、一覧 200・実行 400 を
確かめた(`e2e/scenario-4.md`)。

### 4. 実行時にも検証する

秘密の認可(TOOL-01)を実行のたびに取り直しているのと同じ理由。登録時だけの検証は
「登録後に DB を触られた場合」を素通りさせる。実環境でこれを再現し(実 ADB の行を
UPDATE してから代理実行)、400 で止まることを確かめた(`e2e/scenario-4.md`)。

## 判断を上げる点(ADR-0023 §4・人間レビュー必須)

**固定ヘッダの値は DB に平文で保存される。** 秘密は従来どおり Vault 参照に限る、を契約として
明記するか(明記案が ADR の提案)。併せて、一覧 API で**値を返さず名前だけ**返す案(実装済みの
既定)を採るか、TOOL-01 の URL と同じく値も返すかの採否。影響の比較は ADR-0023 §4 の表。

## 検証で作った資源と片付け

- Vault 秘密 `jetuse-spike-tool02-apikey`(使い捨てトークン。freeform タグ
  `jetuse_spike_run=JETUSE_TOOL02_6BB0A7` でこの run の所有を証明できる)
- ADB スキーマ `JETUSE_TOOL02_6BB0A7`

片付け: `spikes/tool02/teardown.py --yes`(印が一致する秘密だけ削除予約)と
`spikes/ragm02/teardown.py --yes`(所有台帳ゲートつきのスキーマ削除)。
TOOL-01 では「名前が固定で所有を証明できない」ため Vault 秘密を自動削除できなかったが、
本タスクでは**作成時に run の印を付ける**ことでその制約を解いた。

# E2E-7: Internal リリース点の統合確認

対象: `internal-dev` = `ee142e5`（`internal-v0.1.0` の候補点）
環境: 実 OCI / `jetuse:dev` / **us-chicago-1** / 自分の app スタック（`jetuse-sogawa-api`）
モック不使用。実際に配備して HTTP を叩いている。

## なぜ必要だったか

`internal-stable` は 2026-07-06 から止まっており、PR #132 で追随させたときに
「リリース単位の E2E が未実施」としてタグを保留していた。稼働していた Internal 環境
（共有 `jetuse-dev-app`）は **146コミット遅れ**の `6cf875d`（2026-07-23）で、
AGT-04/05/06・TOOL-01/02/03・PREP-03 等を含んでいなかった。**そこでの E2E は
リリース点を検証しない。** そのため、リリース点のコードで配備し直した。

## 配備

| 項目 | 結果 |
|---|---|
| ビルド | `engine=docker platform=linux/amd64`（PR #139・#144 の成果） |
| 稼働イメージ | `jetuse-dev-api:dev-sogawa-ee142e5` = `internal-dev` の HEAD と一致 |
| terraform | `Apply complete! Resources: 1 added, 1 changed, 0 destroyed.` |
| URL | API Gateway（us-chicago-1）で公開 |

## 疎通と機能

| 経路 | 結果 |
|---|---|
| `GET /` | 200 |
| `GET /api/health` | 200 |
| `GET /api/chat/models` | 200 / **11モデル** |
| `GET /api/capabilities` | 200 |
| `GET /api/chat/ping`（SSE） | ストリーム受信 OK |

### 実推論（AGT-06 のシカゴ移行の成果を実測）

```
POST /api/chat/stream  model=gpt-oss-120b  "1+1は？数字だけ"
  data: {"delta": "2"}   usage: in=74 out=54   [DONE]

POST /api/chat/stream  model=grok-4.3       "1+1は？数字だけ"
  data: {"delta": "2"}   usage: in=199 out=109  [DONE]
```

**Grok 系が使えるのはシカゴ移行(AGT-06)の成果。** 大阪では利用できなかった（ADR-0001）。
移行前の稼働環境ではモデル5本だったが、リリース点では11本になっている。

### capabilities の内訳

| 機能 | 状態 | 判定 |
|---|---|---|
| chat | ok（11モデル） | PASS |
| rag | ok | PASS |
| dbchat | ok / `select_ai=true` | PASS |
| ocr | ok | PASS |
| tts | ok / `region=us-phoenix-1`（フォールバック動作） | PASS |
| speech | unavailable / `SPEECH_BUCKET 未設定` | **設定由来**（不具合ではない） |
| agents | disabled / ホスト型未配備・`auth_required=false` | **設定由来** |
| dbchat.semantic_store | false / `SEMSTORE_OCID 未設定` | **設定由来** |

`/api/health` の集約 `ok` は `false` になるが、これは**未設定の任意機能を含めた集約**であり、
このスタックの構成どおり。回帰ではない。

### 内部固有コードが配備されていることの確認

| 経路 | 結果 | 意味 |
|---|---|---|
| `GET /api/builder/sessions` | **405** Method Not Allowed | ルートは登録済み（SP3 ビルダーのコードが入っている） |
| `GET /api/demos` | 503 `database unavailable` | 下記 |

## 確認できなかったこと（正直な限界）

**デモ基盤（SP1〜SP3）の DB 経路は未検証。** `/api/demos` が
`database unavailable` を返す。原因は、**自分の個人 app スタックの ADB スキーマに
内部固有 migration（`017_demos_v2` 以降）が適用されていない**こと。
個人スタックは「API + Gateway + SPA バケット」の最小構成で、デモ基盤用に構成していない。

- ローカルからの `python -m jetuse_core.migrate` は `DPY-4005`（`.env` が指す**大阪の ADB が
  STOPPED**）で届かない。アプリが使うのは**シカゴの `jetuse-dev-adb`**（AVAILABLE）で、
  そちらへ流すには別途スキーマ準備が要る
- デモ基盤の統合確認は、**共有の Internal 環境**（`jetuse-dev-app` + 専用 ADB + IdP）で
  行うのが本来の形

各機能は個別に実環境 E2E を通しており、`docs/verification/demo-platform/` に16本、
`docs/verification/jetuse-app/` に98本の検証レポートが残っている。**今回確認したのは
「リリース点のコードが実際に動くこと」であり、「デモ基盤の統合動作」は範囲外。**

## 途中で見つけて直した配備経路の欠陥（2件）

この E2E に到達するまでに、配備経路が2箇所壊れていた。どちらもローカル
（Apple Silicon / docker）だけが踏む経路で、CI（ubuntu / x86）では露見しなかった。

1. `podman` 直書き → `podman: command not found`（PR #139）
2. `--platform` 未固定 → arm64 イメージを Container Instance が拒否。**しかも置換なので
   旧インスタンスは削除済みで、環境が落ちたまま復旧できなかった**（PR #144）

加えて、`ops/dev-env-up.sh` の namespace フォールバックが `set -e` + `pipefail` で
到達不能だった（PR #142）。**E2E を取ろうとしたこと自体が、3件の実バグを見つけた。**

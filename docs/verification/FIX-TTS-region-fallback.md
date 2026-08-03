# 検証レポート: TTS のリージョンフォールバックが 5xx で働かない不具合

- 対象: `packages/api/jetuse_core/tts.py` の `synthesize()`
- 発見: 2026-08-03、Public版を `jetuse:test` / us-chicago-1 へ配備した際の受け入れ E2E（39項目中この1件のみ FAIL）
- 症状: `POST /api/tts` が常に 503（`音声合成に失敗しました: InternalError Internal Server Error`）。
  一方 `/api/health` の TTS は `{"status":"ok","region":"us-phoenix-1","verified":true}` を返す

## 結論: フォールバック条件が 4xx 限定だったため、動くリージョンに到達していなかった

`synthesize()` は候補リージョンを順に試すが、次の候補へ進むのは `ServiceError` の
status が **401/403/404 のときだけ**で、それ以外は即座に例外を投げていた。
今回はデプロイリージョン（シカゴ）が **500** を返したため、そこで打ち切られていた。

## 実機での切り分け（ユーザープリンシパル・同一パラメータ）

`TTS_2_NATURAL` / voice=`Yuki` / `ja-JP` / MP3 / compartment=`jetuse:test`:

| region | `list_voices` | `synthesize_speech` |
|---|---|---|
| us-phoenix-1 | OK（ja-JP 5ボイス） | **OK**（6783 bytes） |
| us-chicago-1 | **500 `Connection refused: <OCI_INTERNAL_ENDPOINT>`** | 到達せず |
| us-ashburn-1 | OK | OK（6783 bytes） |

→ **Phoenix は正常。シカゴのみ OCI 側の内部障害**（アプリの問題でも IAM 不足でもない）。

## なぜ health だけ緑になったか

`/api/tts` は API Gateway 経由で **Functions ルーター**が処理し（`fn_router_segments = ["presets","dbchat","tts"]`）、
`/api/health` は **Container Instance** が返す。プロセスが分かれているため:

| | 処理プロセス | `_resolved_region` | 候補順 | 結果 |
|---|---|---|---|---|
| `/api/tts` | Functions | なし（起動直後） | シカゴ → Phoenix | シカゴの500で打ち切り → **503** |
| `/api/health` | Container Instance | プローブが Phoenix に解決 | Phoenix → シカゴ | **ok / verified:true** |

health のプローブは `list_voices`（課金なし）で判定するため、Phoenix で成功して緑になる。
「health は緑なのに合成だけ失敗する」という食い違いはこの構造から生じていた。
これは FIX-58 で記録済みの「処理プロセスの分裂」（`docs/tips.md` 2026-07-28）の未対処部分にあたる。

## 修正

1. **5xx でも次の候補へ進む**（`e.status in (401, 403, 404) or e.status >= 500`）。
   401/403/404 = そのリージョンでは未購読/未提供、5xx = リージョン側の障害。
   どちらも「この候補では合成できない」だけで、他の候補は試す価値がある。
   残りの 4xx（400 等）はリクエスト自体の誤りなので従来どおり即打ち切る（無駄な再試行を増やさない）。
2. **全滅時のヒントに直近の HTTP status / code を添える**。従来は「未購読の可能性」だけを出しており、
   実際には一時障害なのに購読設定を疑わせて切り分けが遠回りになる。

## 検証結果

### 実 OCI Speech に対する前後比較（モックなし）

配備時の Functions プロセスと同じ状態（`_resolved_region=None` / `OCI_REGION=us-chicago-1` /
`TTS_REGION` 未指定）から `synthesize()` を実行。候補はどちらも `['us-chicago-1','us-phoenix-1']`。

| | 結果 |
|---|---|
| 修正前 | **FAIL** `TtsError: 音声合成に失敗しました: InternalError Internal Server Error` |
| 修正後 | **OK** 18015 bytes / 先頭 `b'ID3'`（MP3）/ 成功リージョン `us-phoenix-1` |

修正後のログ: `TTS unavailable in us-chicago-1 (HTTP 500 InternalError); trying next region`

### 単体テスト

`packages/api/tests` 896 passed（カバレッジ 73.63%）、`ruff` 緑。回帰テストを4本追加:

- 5xx で次リージョンへ回り成功すること、成功リージョンが記録されること
- 400 では他リージョンを試さず打ち切ること
- 全滅時のヒントに直近の HTTP status / code と「一時障害」の可能性が載ること

## 未実施（完了主張の範囲）

**配備済みスタックの API Gateway → Functions 経路は通していない。** `/api/tts` を担うのは
fn-router のコンテナ画像で、修正の反映には再ビルドが要る。Public版ではそれを `main` への
merge 時に release.yml が行うため、merge 前に配備経路を通すことは通常の手順の中では成立しない。

したがって本レポートは **ライブラリ層を実サービスに対して検証したところまで**を裏づけとする。
merge 後に画像が生成されたら、スタックを更新して `POST /api/tts` が 200 で MP3 を返すことを
確認すること。証跡は `runs/2026-08-03T1600_FIX-TTS/e2e/`（`SKIPPED.md` に制約を明記）。

## 残る未解明

**シカゴの TTS が 500 を返す理由そのものは特定していない。** エラー本文が OCI 内部サービスへの
接続拒否を示しており、サービス側の問題の可能性が高い。本修正はその状況でも機能を落とさない
ようにするもので、シカゴでの合成を復旧させるものではない。

---
id: ER-0018
title: 共有 ADB が予告なく停止していた
status: parked
size: S
source: 実害
created: 2026-08-05
ticket:
pr:
---

## ひとことで

開発環境の DB が止まっていて、原因も再発防止も分かっていない。

## 何が起きたか

2026-08-05、配備後の動作確認中に `GET /api/demos` が 503、`dbchat` が `unavailable` になった。
切り分けたところ **`jetuse-dev-adb`（us-chicago-1）が STOPPED** だった。
ローカルからの直接接続も `DPY-4005` で失敗していた。

起動して `AVAILABLE` にしたところ**即座に復旧**した。アプリ側の問題ではない。

## 分かっていないこと

- **誰が / 何が止めたのか。** `is-auto-scaling-enabled: False` / `is-free-tier: False` なので、
  Free Tier の自動停止ではない
- **再発するのか。** 一度きりか、定期的に起きるのか

同じ日に、`.env` が指す**大阪の ADB も STOPPED** だった（こちらは以前から）。
シカゴ移行（AGT-06）後に大阪側を止めたのは意図的かもしれないが、記録が無い。

## 何が困るか

**症状が「アプリの不具合」に見える。** 実際 `/api/demos` の 503 を最初は
migration 未適用（ER-0015）と判断し、切り分けに時間を使った。
DB が止まっているという可能性が手順のどこにも書かれていない。

## どう直すか

1. **停止の経緯を確認する**（OCI の監査ログ / Events で誰が `StopAutonomousDatabase` を呼んだか）
2. 意図的な運用なら、**その方針を `docs/guides/dev-environments.md` に書く**
   （例:「夜間は止める。使う前に `oci db autonomous-database start` する」）
3. 意図的でないなら、止まらないようにする
4. **切り分けを速くする。** `/api/health` の `dbchat` が `unavailable` のとき、
   ADB の lifecycle-state を確認する手順を troubleshooting に足す

## やらない場合の代償

同じ症状を見るたびに、アプリの不具合を疑って時間を使う。
共有環境なので、止まっている間は全員が影響を受ける。

## 関連

- `runs/2026-08-04T1832_FINISH-4/e2e/scenario-8-containerfile-layers.md`（切り分けの記録）
- ER-0015（同じ 503 を出す別の原因。混同しやすい）

# 検証用リソースの片付け

`spikes/ragm02/teardown.py --yes` を `SPIKE_SCHEMA_PREFIX=JETUSE_RAGM04` /
`SPIKE_HOME=/tmp/jetuse-ragm04` で実行。台帳と実機が USER_ID / 作成時刻 / マーカーの
3 点で一致したときだけ DROP する。**2 つとも削除済み**:

- `JETUSE_RAGM04_C5F34C`（シナリオ2 で使用）… 下記の出力
- `JETUSE_RAGM04_F87BE4`（シナリオ1 の生ログ取得で使用）… 同じ手順で DROP。
  `ACL: DROP USER で同時に消えた（残 0 件）/ 削除後の再照会: ユーザー 0 件 / ACL 0 件`

```
接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=G912A29DFC5DE89_JETUSELOOP2 / DSN=jetuseloop2_low
  台帳と一致（USER_ID / 作成時刻 / マーカーの 3 点）
  削除対象のオブジェクト: [('INDEX', 38), ('INDEX PARTITION', 2), ('TABLE', 21), ('TABLE PARTITION', 1)]
== DROP USER JETUSE_RAGM04_C5F34C CASCADE
  ACL: DROP USER で同時に消えた（残 0 件）
  削除後の再照会: ユーザー 0 件 / ACL 0 件（どちらも 0 が期待値）
  ローカルの認証資材を削除: secrets.json, ledger.json, schema.txt, wallet/
```

- ADB は増やしていない（共有 loop ADB にスキーマだけ作って消した）。
- 検証で作ったファイル（台帳行・ADB チャンク）はスキーマごと消えた。
  マネージド側（Files API / Vector Store）には**何も作っていない**（SKIPPED.md 1 のとおり
  アップロード経路を通せなかったため）。
- ローカルで起動していた API（uvicorn）と SPA dev server は停止済み。
  接続情報を書いた一時ファイルはリポジトリ外のスクラッチにのみ置き、コミットしていない。

# 検証用リソースの片付け

`spikes/ragm02/teardown.py --yes`。スキーマ名は **run 固有**（`JETUSE_RAGM02_<乱数>`）で、
台帳と実機が USER_ID / 作成時刻 / マーカーの 3 点で一致し、`DROP USER` の直前にも
再照合したときだけ削除する。

```
接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=G912A29DFC5DE89_JETUSELOOP2 / DSN=jetuseloop2_low
  台帳と一致（USER_ID / 作成時刻 / マーカーの 3 点）
  削除対象のオブジェクト: [('INDEX', 43), ('INDEX PARTITION', 2), ('TABLE', 23), ('TABLE PARTITION', 1)]

==============================================================================
== DROP USER JETUSE_RAGM02_40D75E CASCADE
==============================================================================
  ACL: DROP USER で同時に消えた（残 0 件）
  削除後の再照会: ユーザー 0 件 / ACL 0 件（どちらも 0 が期待値）
  ローカルの認証資材を削除: secrets.json, ledger.json, schema.txt, wallet/
EXIT=0
```

## 片付け後の共有 ADB の状態

`JETUSE%` のスキーマ一覧（RAGM-02 の検証用スキーマが 1 つも残っていないこと）:

```
JETUSE_SP1_02 | created 2026-07-06 01:44
JETUSE_SP1_03 | created 2026-07-06 02:01
JETUSE_SP2_01 | created 2026-07-06 11:00
JETUSE_SP2_02 | created 2026-07-07 05:19
JETUSE_SP2_02_Q | created 2026-07-07 05:19
JETUSE_APP | created 2026-07-07 08:30
JETUSE_APP_Q | created 2026-07-07 08:30
```

- `JETUSE_RAGM02%` の残存 ACL: **0 件**（期待 0）
- 50,000 行の検証表・E2E のデータ・3 表すべてスキーマごと削除
- ローカルの認証資材（mTLS ウォレット・スキーマのパスワード・台帳・スキーマ名）も削除

## 他タスクの資源に触れていないことの確認

本タスクのスクリプトが打つ `DROP USER` は 1 か所だけで、対象はこの run が作ったスキーマ:

```
spikes/ragm02/teardown.py:9:台帳との 3 点照合を開始時と `DROP USER` 直前の 2 回行う（fail-closed）。
spikes/ragm02/teardown.py:82:        print(f"\ndry-run。実削除は --yes を付けて実行する（DROP USER {SCHEMA} CASCADE）")
spikes/ragm02/teardown.py:85:    banner(f"DROP USER {SCHEMA} CASCADE")
spikes/ragm02/teardown.py:97:    cur.execute(f"DROP USER {SCHEMA} CASCADE")
spikes/ragm02/teardown.py:102:        print("  ACL: DROP USER で同時に消えた（残 0 件）")
spikes/ragm02/guard_checks.py:3:`teardown.py` は `DROP USER ... CASCADE` を打つ。名前だけを根拠にすると、
spikes/ragm02/guard_checks.py:190:  なお「最終照合と `DROP USER` の間に別主体が同名で作り直す」窓は、**スキーマ名を run 固有に
```

それ以外の `DROP TABLE` / `DROP INDEX` は、いずれもその run 固有スキーマで接続した
セッションの非修飾名（= 自スキーマ内）に限られる。

> 共有 loop ADB 自体（`jetuse-loop-adb`）は停止していない。本タスクからは起動
> （STOPPED → AVAILABLE）のみ行い、停止はしていない。ADB は増やしていない。

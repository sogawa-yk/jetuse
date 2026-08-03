# 検証用リソースの片付け（2026-08-01・ADR-0022 保留の判断を受けて）

MEM-01 は実装に進まないので、調査で作ったものをすべて消し、
**共有 loop ADB が検証前の状態に戻っていること**を再照会で確認した。

| 手順 | 証跡 |
|---|---|
| 削除前の状態 | `1-before.txt` |
| dry-run（既定。何も消さない） | `2-dryrun.txt` |
| 実削除（`teardown.py --yes`） | `3-delete.txt` |
| **削除後の再照会** | `4-after.txt` |

## 消したもの

- 共有 loop ADB（`jetuse-loop-adb`）の run 固有スキーマ **`JETUSE_MEM01_EFC2EA`**
  （`DROP USER ... CASCADE`）。台帳との 3 点照合（USER_ID / 作成時刻 / マーカー）を
  開始時と DROP 直前の 2 回通してから削除している。
- そのスキーマに付いていたホスト ACL 3 件（`DROP USER` で同時に消えた）。
- ローカルの認証資材 `/tmp/jetuse-mem01/`（`secrets.json` / `ledger.json` / `schema.txt` / `wallet/`）。
- 調査用の使い捨て venv `/tmp/mem01-venv`（`oracleagentmemory` 一式）。
  **リポジトリの `.venv` には一切入れていない**（依存は増えていない）。
- 調査中の一時ファイル一式（`/tmp/mem01-*`）。

## 削除前 → 削除後

| | 削除前 | 削除後 |
|---|---|---|
| `all_users`（当該スキーマ） | 1 | **0** |
| `all_objects`（当該スキーマ所有） | 28 | **0** |
| `dba_host_aces`（principal = 当該スキーマ） | 3 | **0** |
| `dba_scheduler_jobs`（TTL purge ジョブ） | 1 | **0** |
| `dba_recyclebin` | — | **0**（ごみ箱にも残っていない） |

**共有 ADB 上の他のスキーマは 1 つも触っていない。** `JETUSE*` ユーザーの一覧が
削除前後で「`JETUSE_MEM01_EFC2EA` が消えただけ」であることを示す:

```
削除前: JETUSE_APP, JETUSE_APP_Q, JETUSE_MEM01_EFC2EA, JETUSE_SP1_02,
        JETUSE_SP1_03, JETUSE_SP2_01, JETUSE_SP2_02, JETUSE_SP2_02_Q
削除後: JETUSE_APP, JETUSE_APP_Q,                     JETUSE_SP1_02,
        JETUSE_SP1_03, JETUSE_SP2_01, JETUSE_SP2_02, JETUSE_SP2_02_Q
```

## ADB 本体

`jetuse-loop-adb` は調査開始時に `STOPPED` だったものを起動して使ったので、
**`STOPPED` へ戻した**（`lifecycle-state=STOPPED` を再照会で確認）。ADB は増やしていない。

## 残したもの（意図的）

`docs/verification/MEM-01.md`（実測レポート）と `spikes/mem01/`（再現スクリプト）は残す。
ADR-0022 の再開条件を満たしたときに、ここから再開できるようにするため。

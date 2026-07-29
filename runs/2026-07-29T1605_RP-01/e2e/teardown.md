# 片付けと再照会

所有権（USER_ID＋作成時刻＋マーカー）を開始時・DROP 直前・各 DROP の直前の 3 段で照合し、一致した場合だけ削除する。削除後は再照会して不在を確認し、失敗があれば非ゼロ終了する。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/driver.py teardown
== 片付け ==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  所有権の照合: JETUSE_RP01D389 はこの run が作ったもの（USER_ID=418・作成時刻・マーカーが一致）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  dropped JETUSE_SPIKE_RP01D389_IDX
  dropped JETUSE_SPIKE_RP01D389_PROF
  skip JETUSE_SPIKE_RP01D389_BADCRED: ORA-20004: Credential "JETUSE_RP01D389"."JETUSE_SPIKE_RP01D389_BADCRED" does not exist
  確認: user_tables に JETUSE_SPIKE_RP01D389_IDX$VECTAB は 0 件
  確認: user_cloud_ai_profiles に JETUSE_SPIKE_RP01D389_PROF は 0 件
  確認: user_credentials に JETUSE_SPIKE_RP01D389_BADCRED は 0 件
  dropped bucket jetuse-spike-rp01d389-rag
  確認: バケット jetuse-spike-rp01d389-rag の存在=False
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  DROP 直前の再照合: JETUSE_RP01D389 はこの run が作ったもの（USER_ID=418・作成時刻・マーカーが一致）
  dropped user JETUSE_RP01D389
  dropped user JETUSE_RP01D389_Q
  確認: ユーザー JETUSE_RP01D389 の存在=False
  確認: ユーザー JETUSE_RP01D389_Q の存在=False
done（作ったものはすべて削除され、再照会でも見つからない）
  ウォレットとパスワードファイルを削除
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `teardown.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

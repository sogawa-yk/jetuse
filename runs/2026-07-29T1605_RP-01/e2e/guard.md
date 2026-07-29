# 否定テスト: 台帳が一致しないときは何も消さない

台帳（所有証跡）を 5 通りに壊して片付けを走らせ、いずれも非ゼロ終了し、検証用スキーマ・読取専用ユーザー・マーカーが無傷のままであることを確認する。USER_ID の 2 ケースは「同じ秒内に DROP → 同名で再作成」に相当し、作成時刻では区別できない競合を捉える。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/driver.py guard
== 否定テスト: 台帳が一致しないときは何も消さない ==

  [マーカーが違う] teardown の exit=1
      所有権の照合: JETUSE_RP01D389 のマーカーが台帳と一致しない
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  [マーカーが違う] ユーザー健在={'JETUSE_RP01D389': True, 'JETUSE_RP01D389_Q': True} / マーカー健在=True

  [作成時刻が違う] teardown の exit=1
      所有権の照合: JETUSE_RP01D389 の作成時刻が台帳と違う（作り直されている）
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  [作成時刻が違う] ユーザー健在={'JETUSE_RP01D389': True, 'JETUSE_RP01D389_Q': True} / マーカー健在=True

  [読取専用ユーザーの作成時刻が違う] teardown の exit=1
      所有権の照合: JETUSE_RP01D389 はこの run が作ったもの（USER_ID=418・作成時刻・マーカーが一致）
      - JETUSE_RP01D389_Q の作成時刻が台帳と違う（作り直されている）。1 件も DROP しない
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  [読取専用ユーザーの作成時刻が違う] ユーザー健在={'JETUSE_RP01D389': True, 'JETUSE_RP01D389_Q': True} / マーカー健在=True

  [アプリスキーマの USER_ID が違う（同秒での作り直し相当）] teardown の exit=1
      所有権の照合: JETUSE_RP01D389 の USER_ID が台帳と違う（-1 → 418）＝作り直されている
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  [アプリスキーマの USER_ID が違う（同秒での作り直し相当）] ユーザー健在={'JETUSE_RP01D389': True, 'JETUSE_RP01D389_Q': True} / マーカー健在=True

  [読取専用ユーザーの USER_ID が違う（同秒での作り直し相当）] teardown の exit=1
      所有権の照合: JETUSE_RP01D389 はこの run が作ったもの（USER_ID=418・作成時刻・マーカーが一致）
      - JETUSE_RP01D389_Q の USER_ID が台帳と違う（-1 → 419）＝作り直されている。1 件も DROP しない
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  [読取専用ユーザーの USER_ID が違う（同秒での作り直し相当）] ユーザー健在={'JETUSE_RP01D389': True, 'JETUSE_RP01D389_Q': True} / マーカー健在=True

  台帳を元に戻した
判定: PASS（stale な台帳では破壊操作に入らない）
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `guard.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

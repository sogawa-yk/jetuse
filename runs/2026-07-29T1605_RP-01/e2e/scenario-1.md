# シナリオ1: 有効化と冪等性

ops/setup-dev-schema.py と ops/setup-select-ai.py を同じ検証用スキーマへ 2 回連続実行し、毎回成功すること・OCI$RESOURCE_PRINCIPAL の EXECUTE が付いていること・migrate 済みの表が保たれることを確認する。1 回目は --require-new、両回とも --receipt 付き（CREATE と同じ PL/SQL で取得した user_id が作成証跡として出る）。

```console
$ .venv/bin/python -u runs/2026-07-29T1605_RP-01/e2e/driver.py 1
== シナリオ1: 検証用スキーマへの適用と冪等性 ==
  対象 ADB: jetuse-loop-adb / jetuse:dev（OCID 一致）/ AVAILABLE
  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  実行前: JETUSE_RP01D389 存在=0 / OCI$RESOURCE_PRINCIPAL EXECUTE=0

---- setup-dev-schema.py 1 回目 ----

$ .venv/bin/python ops/setup-dev-schema.py --dev rp01d389 --app-password <PASSWORD> --query-password <PASSWORD> --receipt /tmp/rp01_wallet_79695cce2836/setup-receipt.json --require-new
applied: ['001_init', '002_presets', '003_oci_conversation', '004_usecases', '005_rag', '006_mcp_servers', '007_agents', '008_agent_auto_tools', '009_minutes', '010_agent_framework', '011_audit', '012_normalize_framework', '013_installed_plugins', '014_usecase_source', '015_agent_source', '016_demos']
== ADMIN: ensure JETUSE_RP01D389 / JETUSE_RP01D389_Q ==
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  receipt: JETUSE_RP01D389（user_id=418 / 作成=True）
  created JETUSE_RP01D389
  grants: DBMS_CLOUD, DBMS_CLOUD_AI, DBMS_CLOUD_AI_AGENT, DBMS_CLOUD_PIPELINE -> JETUSE_RP01D389
  ACL: inference.generativeai.<region>.oci.oraclecloud.com
  ACL: generativeai.<region>.oci.oraclecloud.com
  ACL: objectstorage.<region>.oraclecloud.com
  receipt: JETUSE_RP01D389_Q（user_id=419 / 作成=True）
  created JETUSE_RP01D389_Q
  resource principal: JETUSE_RP01D389 -> OCI$RESOURCE_PRINCIPAL

== 認証情報（infra/terraform/environments/app/<dev>.tfvars へ） ==
  adb_user       = "JETUSE_RP01D389"
  adb_query_user = "JETUSE_RP01D389_Q"
  ADB_PASSWORD       (JETUSE app)  = <実行時に指定した値>
  ADB_QUERY_PASSWORD (JETUSE read) = <実行時に指定した値>
== migrate -> schema JETUSE_RP01D389 ==

== done ==
次の手順: 上記の値を <dev>.tfvars の adb_user / adb_query_user / api_environment(ADB_PASSWORD, ADB_QUERY_PASSWORD) に設定
その後: ops/dev-env-up.sh rp01d389
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  台帳に記録: JETUSE_RP01D389（user_id=418 / 作成時刻 2026-07-29 11:50:30）
  台帳に記録: JETUSE_RP01D389_Q（user_id=419 / 作成時刻 2026-07-29 11:50:31）
  この run のマーカーを記録: JETUSE_RP01D389.RP01_RUN_MARKER

---- setup-select-ai.py 1 回目 ----

$ .venv/bin/python ops/setup-select-ai.py --schema JETUSE_RP01D389
== ADMIN: grants + ACL + resource principal (JETUSE_RP01D389) ==
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  db version: 23.26.3.1.0
  grants: DBMS_CLOUD, DBMS_CLOUD_AI, DBMS_CLOUD_AI_AGENT, DBMS_CLOUD_PIPELINE -> JETUSE_RP01D389
  ACL: inference.generativeai.<region>.oci.oraclecloud.com
  ACL: generativeai.<region>.oci.oraclecloud.com
  ACL: objectstorage.<region>.oraclecloud.com
  resource principal: JETUSE_RP01D389 -> OCI$RESOURCE_PRINCIPAL
done
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  1 回目終了: JETUSE_RP01D389 作成時刻=2026-07-29 11:50:30 / OCI$RESOURCE_PRINCIPAL EXECUTE=1 / スキーマ内の表=15（migrate 済み 14 + マーカー 1）

---- setup-dev-schema.py 2 回目 ----

$ .venv/bin/python ops/setup-dev-schema.py --dev rp01d389 --app-password <PASSWORD> --query-password <PASSWORD> --receipt /tmp/rp01_wallet_79695cce2836/setup-receipt.json
applied: (none — up to date)
== ADMIN: ensure JETUSE_RP01D389 / JETUSE_RP01D389_Q ==
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  receipt: JETUSE_RP01D389（user_id=418 / 作成=False）
  JETUSE_RP01D389 は既存。指定パスワードは現行値(ORA-28007・実ログインで確認)
  grants: DBMS_CLOUD, DBMS_CLOUD_AI, DBMS_CLOUD_AI_AGENT, DBMS_CLOUD_PIPELINE -> JETUSE_RP01D389
  ACL: inference.generativeai.<region>.oci.oraclecloud.com
  ACL: generativeai.<region>.oci.oraclecloud.com
  ACL: objectstorage.<region>.oraclecloud.com
  receipt: JETUSE_RP01D389_Q（user_id=419 / 作成=False）
  JETUSE_RP01D389_Q は既存。指定パスワードは現行値(ORA-28007・実ログインで確認)
  resource principal: JETUSE_RP01D389 -> OCI$RESOURCE_PRINCIPAL

== 認証情報（infra/terraform/environments/app/<dev>.tfvars へ） ==
  adb_user       = "JETUSE_RP01D389"
  adb_query_user = "JETUSE_RP01D389_Q"
  ADB_PASSWORD       (JETUSE app)  = <実行時に指定した値>
  ADB_QUERY_PASSWORD (JETUSE read) = <実行時に指定した値>
== migrate -> schema JETUSE_RP01D389 ==

== done ==
次の手順: 上記の値を <dev>.tfvars の adb_user / adb_query_user / api_environment(ADB_PASSWORD, ADB_QUERY_PASSWORD) に設定
その後: ops/dev-env-up.sh rp01d389
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low

---- setup-select-ai.py 2 回目 ----

$ .venv/bin/python ops/setup-select-ai.py --schema JETUSE_RP01D389
== ADMIN: grants + ACL + resource principal (JETUSE_RP01D389) ==
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  db version: 23.26.3.1.0
  grants: DBMS_CLOUD, DBMS_CLOUD_AI, DBMS_CLOUD_AI_AGENT, DBMS_CLOUD_PIPELINE -> JETUSE_RP01D389
  ACL: inference.generativeai.<region>.oci.oraclecloud.com
  ACL: generativeai.<region>.oci.oraclecloud.com
  ACL: objectstorage.<region>.oraclecloud.com
  resource principal: JETUSE_RP01D389 -> OCI$RESOURCE_PRINCIPAL
done
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low
  2 回目終了: JETUSE_RP01D389 作成時刻=2026-07-29 11:50:30 / OCI$RESOURCE_PRINCIPAL EXECUTE=1 / スキーマ内の表=15（migrate 済み 14 + マーカー 1）
  接続先確認: jetuse-loop-adb / compartment=dev（承認済み）/ DB_NAME=<DB_TOKEN>_JETUSELOOP2 / DSN=jetuseloop2_low

判定: PASS（2 回連続実行しても成功。RP 付与・スキーマ・マイグレーション適用が保たれる）
```

> 実行順は 1 → guard → 2 → 3 → 4 → teardown。作る資源の名前は run 固有（この run は `rp01d389`）。実行ログの原本は `scenario-1.log`（`*.log` は .gitignore 対象）。
> OCID・ネームスペース・リージョンは `spikes/spike_m1/redact_evidence.py` で伏字化済み。

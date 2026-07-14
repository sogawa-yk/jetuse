# E2E 実施状況（全シナリオ完了）

- Scenario 1 (PAR 相対期限 + gateway 200): ✅ 共有スタック apply 後に実施（scenario1-summary.txt）。
  apply job=ocid1.ormjob.oc1.ap-osaka-1.amaaaaaal7l2mtaajd56jjsbnasatlbwbb2vcx675vzvn4l2ukypswhpjgrq SUCCEEDED。fix_wallet_and_restart.sh 実行済（DB READY）。
- Scenario 2 (リージョンガード): ✅ scenario2-summary.txt。
- Scenario 3 (schema 妥当性 + RM plan 成功): ✅ scenario3-summary.txt。
- 補助: prefix-validation.txt / ocir-namespace-guard.txt。

min_scenarios=2 を超過（3本＋2補助）。無言スキップなし・SKIPPED なし。

# E2E スキップ状況（最終更新 2026-07-13）

**スキップなし — 全シナリオ実施済み。** 結果は RESULTS.md、経緯は下記。

- 2026-07-10 時点では IAM 人間ゲート待ちで全シナリオ未実施だった（当時の切り分け記録は
  rm-plan-1-test.log / rm-plan-2-devprobe.log / local-plan-test.log と REVIEWER-CONTEXT.md）。
- 2026-07-13 に人間ゲート通過（jetuse:test 権限 + inspect tenancies + 既存 DG 方式の IAM 手動作成
  = iam-report.md）。大阪 VCN 枠超過のため us-chicago-1 へ切替（tasks/FIX-47.md が事前承認する代替）。
- シナリオ0（再現）/1（クリーンルーム）/2（明示 PROJECT_OCID）/3（ネガティブ）すべて実施・合格。

補足（シナリオ実施上の注記であり未実施ではない）:
- シナリオ1 の初回 health は新規 project の DP 伝播待ち（数分）で一時 ok=false → 再試行で ok=true
  （RESULTS.md 発見4。設計判断: チャット経路を分単位でブロックしないため待ち込みはしない）。
- リージョンは Issue と同条件の大阪でなく ord。修正はリージョン非依存で、再現（シナリオ0）も
  ord 上で成立している（DP 状態 API + project 欠如 → 5xx は同根。報告者は CP 段 404 / 当環境は
  DP 段 400 という段差はあるが、どちらも本修正の表面化+診断の対象）。

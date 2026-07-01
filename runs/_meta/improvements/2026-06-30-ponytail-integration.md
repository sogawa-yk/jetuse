# 改善記録: ponytail スキルをループエンジニアリング枠組みへ統合

- 日付: 2026-06-30
- 起票: loop-doctor（施主依頼「ponytail を既存のループ枠組みで使えるように」）
- 承認: 施主 2026-06-30（提示オプション **A** = P1+P2+P3）
- 対象 run（証跡）: なし＝バグ症状ではなく**枠組みの追加要望**。根拠は現状の仕組みファイルと ponytail 規約本文（下記）。
- 導入スキル: `DietrichGebert/ponytail`（`/ponytail` `/ponytail-review` `/ponytail-audit` `/ponytail-debt` ほか）

## 設計判断（なぜこの配線か）
ponytail には2つの顔があり配線先が異なる。本枠組みは **maker（Claude 実装）→ checker（Codex 採点）** 分離が要。
- **実装規律（`ponytail`）** → loop-protocol の「実装」ステップ＝**maker 側**に効かせる。
- **過剰実装レビュー（`ponytail-review`）** → Codex の**前段**に置く maker 側の自己短縮。**Codex 採点には混ぜない**
  （混ぜると component-map「厳しすぎ→進まない」を誘発し、採点者分離が濁る）。`review_verdict` を動かせるのは Codex だけ。
- 全リポジトリ系（`ponytail-audit` / `ponytail-debt`）→ 毎ターンではなく loop-doctor の診断ツール（今回は配線せず・将来 P4）。

衝突しないことの根拠: ponytail 本文 `skills/ponytail/SKILL.md`「When NOT to be lazy」が
「信頼境界の入力検証・データ損失防止のエラー処理・セキュリティ・明示要求は簡略化しない」と自ら宣言。
本repo の broker fail-closed / 署名検証 / Vault / 必須ドキュメント（ADR・比較・検証・spec, CLAUDE.md 明示要求）と矛盾しない。

## 適用した変更（A = P1+P2+P3）
1. **P1 `.claude/skills/loop-protocol/SKILL.md` 手順2 加筆**: 実装/リファクタ時に `ponytail`（既定強度=config）を
   はしごで適用。ただし fail-closed/署名検証/Vault/入力検証/明示要求/必須ドキュメントは簡略化対象外。
   完了ゲート E2E（`min_scenarios≥2`）は ponytail の「one check」より優先＝弱めない、と明記。
2. **P2 同ファイル 手順2.5 新設**: codex-review の**前に** `ponytail-review` を当て maker 自身が差分を短くする。
   出力は `runs/<run-id>/reviews/ponytail-<n>.txt` に**証跡**として残す。**非採点**（Codex のみ採点）を明記。
3. **P3 `loop-config.yml` に `ponytail:` ブロック追加**（`review:` の後）:
   `enabled: true` / `intensity: full`（ultra は人間明示時のみ）/ `self_review: true`。

## 却下した代替
- `run_codex_review.sh` の INSTRUCTIONS に過剰実装観点を追加する案＝**非推奨で不採用**。
  採点者の基準を変えて minor を blocker 化しループを止める恐れ＋maker/checker 分離が濁る。

## 副作用
- P1: 実装が過度に削られる懸念 → ponytail 自身の例外則＋Codex 正確性レビュー＋E2E ゲートで二重担保。低リスク。
- P2: 1ターンあたり ponytail-review 1パス分のトークン増（小）。「採点と誤認」は "非採点" 明記で回避。
- P3: 設定のみ。スキルが参照して初めて挙動化＝安全。

## 検証（次の loop run で）
- 次の実装ターンで maker が手順2の ponytail はしごを実際に踏み、手順2.5 で `runs/<id>/reviews/ponytail-<n>.txt` を
  残すかを確認する。Codex の `review_verdict` が ponytail-review に影響されていないこと（採点者分離の維持）も確認する。
- fail-closed/署名検証/Vault 周りが ponytail 適用後も簡略化されていないこと（Codex 正確性レビューで担保される想定）。

## 範囲外（今回はやらない）
- P4: `ponytail-audit` / `ponytail-debt` の loop-doctor 診断ツール明記（component-map 追記）。必要時に別途。

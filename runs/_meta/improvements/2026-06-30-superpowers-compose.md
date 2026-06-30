# 改善記録: Superpowers を「合成（C案）」でループ枠組みへ統合

- 日付: 2026-06-30
- 起票: loop-doctor（施主依頼「Superpowers の良さを取り込む」→ 方式比較の末 C を承認）
- 承認: 施主 2026-06-30（A=つまみ食い / B=ハードフォーク / **C=合成** のうち **C**）
- 対象 run（証跡）: なし＝枠組みの追加要望（バグ症状ではない）。根拠は前ターンの比較と Superpowers README。

## なぜフォーク(B)でなく合成(C)か
- B（ハードフォーク）の損: upstream が新スキル寄稿を基本受けない→**永久に手マージする保守税**。Superpowers は
  **助言型スキル**、こちらの価値は**歯のあるゲート**（採点上書き禁止 / 実環境E2E / fail-closed / loop-doctor）で
  哲学が不一致。さらに実環境ゲート等は **OCI/jetuse 密結合**で汎用フレームワークに混ぜると非汎用化する。
- C の得: 両者の関心は**直交**（Superpowers=開発者規律 / 本ループ=強制と運用）。直交は**混ぜる(fork)より重ねる(compose)**
  方が綺麗。スキルは markdown 指示なので**フォークせず invoke で取り込める**。upstream 改善は `/plugin update` で無税追従。
- フォークが正当化されるのは「Superpowers の内部挙動を変えたい」場合のみ。こちらは**外側にゲートを足したい**だけ→合成で足りる。

## 適用した変更（こちら側の配線）
1. `.claude/skills/loop-protocol/SKILL.md` 冒頭に **2層構成（合成）** の節を追加:
   開発者規律は Superpowers を無改造 invoke、ループは「強制と運用の殻」。どのスキルを使うかは loop-config が単一真実源。
2. 同 手順2 に **TDD 配線**: 非自明ロジックは `test-driven-development`（RED-GREEN-REFACTOR）。実環境E2E証跡と相補。
   要件曖昧→`brainstorming` / 多段→`writing-plans` / 不具合→`systematic-debugging` を on-demand reach。
   ponytail(最小化)と TDD(先にテスト)は両立＝「最小だが検証付き」。
3. `loop-config.yml` に `superpowers:` ブロック追加（`ponytail:` の後）:
   `enabled` / `per_turn_skills`=[test-driven-development] / `on_demand_skills`=[brainstorming, writing-plans,
   systematic-debugging, verification-before-completion]。導入コマンドをコメントに明記。

## 施主が行う前提作業（私=エージェントは /plugin を実行不可）
- `/plugin install superpowers@claude-plugins-official`（公式）
  もしくは `/plugin marketplace add obra/superpowers-marketplace && /plugin install superpowers@superpowers-marketplace`
- 機械グローバル install なので worktree 横断で参照可（ponytail と同様）。install 前は loop-protocol のスキル名参照は
  dangling だが無害（install で即活性化）。

## 副作用 / 留意
- per-turn TDD はターンあたりテスト先行の手間増。ただし退行検知の価値が上回る想定。trivial 一行は TDD 不要（YAGNI）。
- 再現性/CI を厳密にしたい場合は将来 **vendoring**（リポジトリへスキル同梱）を別判断（今は最小=install で PoC）。
- ライセンス: Superpowers は MIT。無改造 install のため改変・再配布の論点は発生しない。

## 検証（次の loop run で）
- install 後、1タスクを回して: 手順2で `test-driven-development` の RED-GREEN-REFACTOR が実際に踏まれるか、
  Codex 採点が TDD に影響されず独立しているか（採点者分離の維持）、ponytail との両立（最小かつテスト付き）を確認。
- うまく回れば on_demand_skills（brainstorming/writing-plans）を stage・loop-runner のタスク起票前段にも広げる。

## 範囲外（今回やらない）
- vendoring / CI への組み込み。Superpowers 内部の改変（=フォーク。C の方針上やらない）。
- 汎用「grading-integrity プロダクト」化（別目的。やるならフォークでなくゼロから別プラグイン）。

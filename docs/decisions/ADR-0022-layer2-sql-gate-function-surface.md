# ADR-0022: 層2 SQL ゲートの関数呼び出し面をどう fail-closed にするか

- Status: **Accepted（2026-07-07 人間承認・オーケストレータ経由）** — 選択肢 **B + C** を採用
- Date: 2026-07-07
- Context: SP2-03 / specs/18 §4.3（層2 fail-closed SQL ゲート）/ codex-review round 11 B002

## 決定（2026-07-07 人間承認）

**B（DB レベル境界を正本、層2 はベストエフォート）+ C（段階的硬化）を採用**する。すなわち:
- **層2 の完了条件解釈**: 実データ境界は **VPD + 最小権限クエリユーザー**（任意関数の EXECUTE を持たない）
  が正本。層2 SQL ゲートはその背後の**多層防御**であり、未修飾関数呼び出し面の完全な fail-closed
  （文法パーサ化＝選択肢 A）は SP2-03 の完了条件では**要求しない**。A は別タスクで正当 NL2SQL コーパス
  整備後に判断する。
- **C の実装**: `_FORBIDDEN_FUNCS` に辞書/システム露出の既知組み込み関数
  （`SYS_CONTEXT` / `USERENV` / `ORA_INVOKING_USER(ID)` / `ORA_DATABASE_NAME` / `ORA_DICT_OBJ_*`）を追加。
  いずれも `(` 付き呼び出しのみ拒否＝素の同名列は後方互換で通す。実装済み（sqlguard.py・単体
  test_rejects_dict_system_builtins / test_same_named_bare_column_still_allowed）。

## 背景

層2 SQL ゲート（`packages/jetuse_shared/jetuse_shared/sqlguard.py` の `enforce_sql_boundary`）は
**手書きの単一パス字句解析 + allowlist**で、FROM/JOIN のテーブル参照を SH 修飾表・登録済み DS 表・
DUAL・CTE のみに限定する。round 1〜10 で Oracle 構文の迂回長尾（列挙 synonym / CTE スコープ /
括弧付き JOIN / SH 全オブジェクト / CROSS APPLY / 引用 CTE・パッケージ / PIVOT / XML DB oradb: /
URI 型）を順次塞ぎ、round 10 時点で新規の SQL 迂回指摘はゼロに収束していた。

round 11 で codex は新たに **関数呼び出し面**を指摘した（B002）:
- 未修飾のユーザー定義関数呼び出し `SELECT LEAK_FN() FROM DUAL` がゲートを通過する。
- codex 推奨の修正は「許可する組み込み関数を明示 allowlist 化し、それ以外の call-shaped な
  未修飾名を拒否」＋「FROM 表別名を追跡して既知の alias.column 以外の修飾式も拒否」。

## 問題（なぜ自明な実装判断でないか）

現行トークナイザは演算子（`=` `>` `+` `*` 等）を**トークン化せず読み飛ばす**（`_tokenize` の
`else: i += 1`）。このため `WHERE x = (SELECT ...)` は `ident("X") lparen` に落ち、関数呼び出し
`f(` と**字句上区別できない**。素朴に「未修飾 ident の直後が lparen なら関数」として allowlist 照合
すると、`col = (subquery)` や `a > (b)` を全て「未知関数」と誤判定して 403 になる（後方互換破壊 =
round 10 M001 と同じクラスの回帰）。

さらに allowlist は組み込み関数（COUNT/SUM/TO_CHAR/…）だけでなく、括弧が続きうる**節キーワード**
（WHERE/HAVING/IN/EXISTS/OVER/WITHIN GROUP/ROLLUP/CUBE/PIVOT/…）と、CAST の**データ型**
（`NUMBER(10,2)` `VARCHAR2(20)` `TIMESTAMP(6)` …）まで網羅しないと正当 SQL を壊す。1つでも
漏れると Select AI 生成 SQL が偽陽性 403 になり、これは検出困難な回帰を生む。

修飾ゼロ引数呼び出し `SOME_PACKAGE.LEAK`（括弧なし）は Oracle SQL では**関数呼び出しにならない**
（`<表/別名>.<列>` と解釈され ORA-00904）。括弧付きの修飾呼び出し `X.Y(` は既存のスキーマ修飾
関数チェックで既に 403。したがって「括弧なし修飾ゼロ引数」に対する別名追跡は、**有効 SQL 上の
攻撃面が存在しない**。

つまり codex の要求を正しく満たすには (a) 演算子のトークン化、(b) 式位置の文法的判定、
(c) 組み込み関数＋節キーワード＋データ型の包括 allowlist、(d) クエリブロック単位の 2 パス別名収集、
が必要で、これは字句パーサから**文法パーサへの作り替え**に相当する。回帰リスクが高く、SP2-03 の
受け入れ条件（DemoContext 解決 + dbchat デモスコープ化）を超えた設計判断のため、spec-driven ルール
（CLAUDE.md「仕様にない実装判断は実装せず ADR で人間レビューを要求」）に従いここで停止する。

## 多層防御としての現状評価（この面の実リスク）

層2 ゲートは **VPD（層1）の背後の多層防御**である。実データ境界は VPD が担い、scenario-2 で
別 owner 直接接続時に **0 行**（越境行なし）を実測済み。層2 が守るのは辞書**メタデータ**（表・列名）
であり、行データではない。

`LEAK_FN()` が実際に情報を漏らすには、(1) 別スキーマ所有の definer-rights 関数が存在し、(2) 読取専用
クエリユーザー `JETUSE_..._Q` に EXECUTE が付与され、(3) その本体が辞書/他テナントを読む、の全てが
必要。デモテナンシはそのような関数を作らず、クエリユーザーに任意関数の EXECUTE を与えない。
よって本指摘は**能動的な穴ではなく多層防御の硬化**に属する（ただし fail-closed 設計としては未完）。

## 選択肢

- **A. 文法パーサへ作り替え**: 演算子トークン化 + 式位置判定 + 包括 allowlist + 2 パス別名収集。
  fail-closed を字句レベルで達成するが、実装コスト大・回帰リスク大。既存 F1〜F10 と本 ADR の
  正当 SQL コーパスで退行テストを固める前提。
- **B. DB レベル境界を正本と位置づけ、層2 はベストエフォート**: 実境界を VPD + 最小権限クエリユーザー
  （任意関数の EXECUTE を与えない）とし、層2 は既知危険面のブロックリスト硬化に留める。SP2-03 の
  受け入れ条件は VPD/最小権限で既に満たされる。低リスク・低コスト。
- **C. B の上に段階的硬化**: `_FORBIDDEN_FUNCS` に辞書/システム露出の既知組み込み
  （例 `ORA_DICT_OBJ_*`）を追加し、将来 A をやるならコーパス整備後に着手。

## 推奨

**B（+ C の段階的硬化）**を推奨。理由: 実データ境界は VPD で担保済み（実測 0 行）、クエリユーザーは
任意関数を実行できず、A の回帰リスクは SP2-03 の便益に見合わない。A を採る場合は別タスクとして
「正当 NL2SQL コーパス + Oracle 組み込み/キーワード/型 allowlist」を先に整備し、退行を固めてから
着手すべき。**本判断は人間レビューを要する**（層2 ゲートの完了条件＝fail-closed の解釈に関わるため）。

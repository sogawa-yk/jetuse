# SPIKE-G3 (GAP-03): Code Interpreter相当の方式比較とセキュリティ評価

実施日: 2026-06-15 / リージョン: ap-osaka-1 / 判定軸: **マネージドで安全に実現できるか**

## 目的

native エンジン限定で使えている **コード実行（OCI Responses built-in `code_interpreter`）** を、
Agents SDK / LangGraph エンジンでも使えるようにできるか。
計画(`docs/plan-gap-b.md` GAP-03)のとおり **安全性が最重要** で、安全な方式に確信が持てなければ実装しない。

## 現状（native エンジンのコード実行は何で動いているか）

`packages/api/jetuse_core/chat.py:327` — Responses ストリームの `code_interpreter_call` を
**OCI側サンドボックスで実行される built-in**（承認対象外・通知のみ）として扱っている。
つまり現状のコード実行は **OCIフルマネージドのサンドボックス**であり、アプリ側はコードを一切実行していない。

```python
if itype == "code_interpreter_call":
    # built-in: OCI側サンドボックスで実行される(承認対象外・通知のみ)
    yield {"tool_call": {"name": "code_interpreter", "label": "コード実行", "builtin": True, ...}}
```

## 方式比較

| # | 方式 | マネージドか | 安全性 | 判定 |
|---|---|---|---|---|
| 1 | **OCI Code Interpreter built-in を SDK/LangGraph から再利用** | ✅ フルマネージド | ✅ OCI側サンドボックス | **不可（技術制約）** |
| 2 | 自前サンドボックス実行ツール（別コンテナ/gVisor/サブプロセス+rlimit/制限付interp） | ❌ 自前運用 | ⚠ 任意コード実行リスク | no-go（後述） |
| 3 | OCI Functions を使い捨て実行環境にする | △ Functionsはマネージドだが実行コードは自前管理 | ⚠ 同上＋経路複雑 | no-go（後述） |

### 方式1が不可な理由（技術制約・ADR-0007で確定済み）

OCI のマネージド Code Interpreter は **Responses API の built-in tool としてのみ** 公開されている。
Agents SDK / LangGraph は **chat completions 経由**で動いており（ADR-0007/0008）、
chat completions には code_interpreter / file_search 等の built-in tool が**存在しない**。
SDK の入力形式と OCI Responses の厳格スキーマも不整合（ADR-0007 原因1）で Responses 直結も実用不可。

→ **「マネージドなコード実行」を SDK/LangGraph 経路に持ち込む手段は OCI に無い。**

### 方式2・3が no-go な理由（安全性）

方式2/3 はいずれも **「ユーザー/LLMが生成した任意コードを自分で実行する」** ことになり、
本質的に以下の防御を**自前で**完全に担保する必要がある:

- ネットワーク完全遮断（メタデータエンドポイント169.254.169.254 への到達でインスタンスプリンシパル窃取が起きうる）
- CPU / メモリ / 実行時間 / プロセス数 / FS の隔離とクォータ
- コンテナブレイクアウト・サイドチャネルへの継続的な追従

これは **マネージドサービスでカバーされない領域**であり、本プロジェクトの判定軸（マネージドで導入できる分だけ導入）から外れる。
加えてチェックポイント④（セキュリティレビュー/OSS公開）の観点でも、任意コード実行の自前サンドボックスは
レビュー負荷とリスクが大きい。**安全性に確信が持てないため実装しない**（計画のゲート方針どおり）。

## ゲート判定: **no-go（マネージド軸）**

- マネージドなコード実行は **既に native エンジンで利用可能**。新規に作るべきものは無い。
- SDK/LangGraph 経路へマネージドで持ち込む手段は OCI に存在しない（プラットフォーム制約）。
- 自前サンドボックスはマネージド軸外＋安全性リスクのため不採用。

### 確定する使い分け（正式ルール）

> **コード実行が必要なユースケースは native エンジンを選ぶ。**
> Agents SDK / LangGraph エンジンはコード実行 built-in を持たない（chat completions 制約）。
> これは欠落ではなく、エンジン選択の設計ルールとして確定する。

## comparison/aws-reference.md への反映

GAP-03 は B（簡易版ギャップ）→ **A（プラットフォーム制約）相当**へ降格。
「マネージドのコード実行は native で実装済み。SDK/LangGraph 経路は OCI のプラットフォーム制約で
built-in 不可のため未実装（コード実行が必要なら native を使う、を使い分けとして確定）」と記録。

## 参照

- ADR-0007（Responses built-in が chat completions 経路で使えない根拠）
- `packages/api/jetuse_core/chat.py:327`（native の OCIマネージドサンドボックス利用）

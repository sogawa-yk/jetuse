---
id: ER-0016
title: registry コンパートメントに不要な権限が向いている
status: parked
size: S
source: 気づき
created: 2026-08-05
ticket:
pr:
---

## ひとことで

コンテナイメージを置くだけの場所に、アプリを動かすための権限が向いたままになっている。

## 何が起きているか

`jetuse:registry`（旧 `jetuse:public`）は **OCIR リポジトリ5本しか置かない非実行環境**。
それにもかかわらず、次の2つが向いている。

| 対象 | 内容 |
|---|---|
| `jetuse-internal-dg`（Dynamic Group） | Container Instance / Functions / ADB / Semantic Store を対象に含む |
| Functions 呼び出し用 Policy | `Allow any-user to use functions-family in compartment id <registry>` |

**現時点では効果が無い**（該当リソースが存在しないため）。ただし将来 registry に
Resource Principal が誤って配置されると**自動的に加入し、dev 側と同じ権限を得る**。

## 根拠

2026-08-04 の実測。`jetuse:registry` の中身は OCIR リポジトリのみ。

```
jetuse-api              46 images  public
jetuse-fn-router        45 images  public
jetuse-agent-openai     24 images  public
jetuse-agent-langgraph  24 images  public
jetuse-agent-adk        24 images  public
```

Container Instance / Functions / ADB は **0件**。

`docs/setup/dynamic-group-matching-rules.md` の冒頭に、この論点を
「**未解決（2026-08-04・IAM 変更は人間ゲート）**」として記録済み。

## どう直すか

1. `jetuse-internal-dg` の Matching Rule から `registry` の OCID を外す
2. Functions 用 `any-user` Policy を registry に対して作らない（既にあれば削除）
3. `docs/setup/dynamic-group-matching-rules.md` の「未解決」注記を消し、確定した構成に直す

**IAM 変更は人間ゲート**（CLAUDE.md）。実施前に承認が要る。

## やらない場合の代償

いまは無害だが、**「非実行環境」という前提が崩れた瞬間に権限が自動で付く**。
最小権限の観点で、前提に依存した安全性は避けたい。

## 関連

- `docs/setup/dynamic-group-matching-rules.md`（冒頭の未解決注記）
- ADR-0028（コンパートメントは「壊してよいか」の1軸で切る）

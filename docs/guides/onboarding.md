# 開発者オンボーディングガイド

新しくジョインする開発者が**ローカルでアプリを動かし → テストを通し → ブランチで変更し → 自分専用のクラウド環境でE2E**まで一通りできるようになるための入門。

> このリポジトリの運用ルールの正本は [../../CLAUDE.md](../../CLAUDE.md)。本ガイドはその実践手順版。
> 全体像は [README](../../README.md)・[architecture/system.md](../architecture/system.md)、技術知見は [KNOWLEDGE.md](../KNOWLEDGE.md)。

---

## 0. 前提知識(ざっくり)

- **何のアプリか**: OCI Enterprise AI(OpenAI互換API)を基盤にした社内向け生成AIユースケース基盤(チャット/RAG/DBチャット/エージェント/音声/画像)。
- **構成**: React SPA(静的配信) ＋ FastAPI(SSE系=Container Instance)/OCI Functions(非ストリーミング) ＋ ADB 26ai ＋ OCI Enterprise AI。リージョンは**大阪(ap-osaka-1)**。
- **開発方式**: spec-driven(仕様は `specs/`)・実機検証主義(結果は `docs/verification/`)。詳細は CLAUDE.md。

## 1. 必要なもの

| 種別 | 内容 |
|---|---|
| OCIアクセス | テナンシ/コンパートメント `jetuse-proto` への権限、`~/.oci/config`(DEFAULTプロファイル, APIキー) |
| 秘密値 | `.env`(ADB等の接続情報)。**リポジトリには無い**ので管理者/チームから受領 |
| ツール | Python 3.12 / Node 22 / podman / Terraform 1.15+ / OCI CLI。開発ホスト `dev` には導入済み |

> 開発ホスト(`dev` インスタンス)上で作業する場合、ツールとウォレットは整備済み。手元PCで動かす場合は上記ツールを各自導入する。

## 2. セットアップ(初回)

```bash
git clone <repo>            # GitHub経由
cd jetuse

# --- Python(API) ---
python3.12 -m venv .venv
.venv/bin/pip install -e "packages/api[dev]"   # 実行+開発依存(pytest/ruff/uvicorn)

# --- Node(SPA) ---
cd packages/web && npm install && cd ../..

# --- 環境変数 ---
cp .env.example .env        # 値は管理者/チームから受領して記入(コミット禁止・gitignore済み)
#  ~/.oci/config も用意(IAM署名に使用)
```

ポイント:
- `.env` と `~/.oci/config` の実値は**絶対にコミットしない**(CLAUDE.md)。雛形は `.env.example`。
- ADBは**夜間停止**運用。作業前に起動確認: `bash ops/start-adb-if-stopped.sh`(「送信無反応/保存失敗(503)」の常連原因)。

## 3. ローカルで動かす

2つのプロセスを起動する(別ターミナル)。

```bash
# API(認証オフ・localhost:8000)
cd packages/api && AUTH_REQUIRED=false ../../.venv/bin/uvicorn service.main:app --port 8000

# SPA(/api を localhost:8000 へプロキシ・localhost:5173)
cd packages/web && VITE_AUTH_REQUIRED=false npm run dev
```

- ブラウザは **HashRouter** のため `http://localhost:5173/#/` を開く。
- `AUTH_REQUIRED=false` 時のユーザーは `dev-user` 固定(ログイン不要)。
- DBを使う機能(会話履歴・DBチャット・RAG等)は `.env` のADB接続とADB起動が前提。未整備でも画面は開く(該当機能が503)。

## 4. テストとコミット前チェック(必須)

```bash
# API
.venv/bin/ruff check packages/api && ( cd packages/api && ../../.venv/bin/python -m pytest -q )
# SPA
cd packages/web && npm run build && npm run lint
```

- フロントは `npm run build` 成功まで通す(CLAUDE.md)。
- CI(GitHub Actions `.github/workflows/ci.yml`)でも ruff/pytest/lint/build と `terraform fmt -check`・`validate` が走る(自動デプロイは無し)。
- 既知: 一部の hosted-agent 系テストはクリーン状態でも失敗中(自分の変更が原因かは `git stash` で切り分ける)。

## 5. 変更の進め方(Gitフロー)

- **1タスク = 1ブランチ + PR**。Public または両版向けは `main` から分岐して `main` へ入れ、直後に `main → dev` の同期PRを出す。Internal 固有・先行機能は `dev` から分岐して `dev` のみに入れる。
- `dev` 全体を `main` へ merge しない。Internal 機能を後から Public 化するときは、対象変更だけを最新 `main` 上へ移植する。
- 詳細、hotfix、tag 規約は **[branching-and-releases.md](./branching-and-releases.md)** を参照。
- spec-driven: 仕様にない実装判断が要るときは実装せず `docs/decisions/` にADR案を書いて人間レビューを依頼。
- **人間承認が必要な操作**: 本番相当の `terraform apply`、IAMポリシー変更、Identity Domain設定変更、スパイク用以外のリソース削除。
- 検証用クラウドリソースを自分で作るときは **`jetuse-spike-` プレフィックス必須**。
- コミットメッセージ末尾の Co-Authored-By 等はリポジトリ慣例に従う。

## 6. 自分専用のクラウド環境でE2E(複数人開発の肝)

ローカルで十分に確認したら、**自分専用のデプロイ環境**で実機E2Eする。他の開発者と衝突しないよう、高価な基盤(ADB等)は共有しアプリ層＋専用DBスキーマだけ分ける仕組みがある。

→ 手順は **[dev-environments.md](./dev-environments.md)** を参照。要約:

```bash
.venv/bin/python ops/setup-dev-schema.py --dev <you>     # 専用スキーマ作成(初回)
cp infra/terraform/environments/app/alice.tfvars.example \
   infra/terraform/environments/app/<you>.tfvars         # 値を記入
ops/dev-env-up.sh <you>     # build/push→plan確認→apply→SPA配信→URL表示
ops/dev-env-stop.sh <you>   # 使わない間はCI停止(課金停止)
ops/dev-env-down.sh <you>   # 破棄(共有基盤・スキーマは保持)
```

## 7. 困ったとき

| 症状 | まず疑う |
|---|---|
| 送信が無反応 / 保存失敗(503) | **ADB停止**。`ops/start-adb-if-stopped.sh` |
| 画面が真っ白 | URLが `/#/`(HashRouter)か。コンソールエラー確認 |
| デプロイ後に全リクエスト到達不能(curl 000) | CI再作成でIP変化。**フル `terraform apply`** でGWのbackend反映 |
| コード変更が反映されない | 同じイメージタグは反映されない。タグを変えて再ビルド |
| その他の実機ハマり | **[tips.md](../tips.md)**(時系列の一次情報)/ [KNOWLEDGE.md](../KNOWLEDGE.md) |

## 8. ドキュメント地図

| 目的 | 参照 |
|---|---|
| 運用ルール(正本) | [../../CLAUDE.md](../../CLAUDE.md) |
| ドキュメント索引 | [docs/README.md](../README.md) |
| 全体設計・図 | [architecture/system.md](../architecture/system.md) |
| 技術知見まとめ | [KNOWLEDGE.md](../KNOWLEDGE.md) |
| 実機ハマり集 | [tips.md](../tips.md) |
| 自分専用E2E環境 | [dev-environments.md](./dev-environments.md) |
| Public / Internal Gitフロー | [branching-and-releases.md](./branching-and-releases.md) |
| 計画(正本) | [plan.md](../plan.md) |
| カスタマイズ | [customize.md](./customize.md) |

---

ようこそ。まずは **§2→§3 でローカル起動 → §4 でテスト** を通し、最初の小さな変更を §5 のフローで出してみてください。

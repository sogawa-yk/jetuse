# E2E-8: Containerfile のレイヤ分割

対象: `packages/api/Containerfile` / `.dockerignore`（新規）
実測環境: Apple Silicon / docker 29 / `--platform linux/amd64`（QEMU エミュレーション）

## なぜ必要だったか

`ops/dev-env-up.sh` によるローカル配備が、**コードを1行変えるたびに42分**かかっていた。
原因は Containerfile のレイヤ順で、アプリのソースを COPY してから `pip install .` を
していたため、**ソース変更が依存インストール層を毎回無効化**していた。

```dockerfile
COPY packages/api/jetuse_core ./jetuse_core   # ← ここが変わると
COPY packages/api/service ./service
COPY packages/api/fn ./fn
RUN pip install --no-cache-dir . uvicorn      # ← 全依存を再インストール
```

## 測った値

### ビルド時間（すべて amd64 エミュレーション下の実測）

| 状況 | 分割前 | 分割後 |
|---|---|---|
| 初回（cold） | 2547秒（pip 段階） | **2626秒**（依存 2519秒 + 自パッケージ 83秒） |
| **コードを1行変えて再ビルド** | **2547秒 ≈ 42分** | **82秒**（CACHED 4層） |

初回はほぼ同じ。**変わるのは2回目以降で、42分 → 1分22秒**（約31倍）。

### キャッシュが壊れる頻度（直近90日の変更回数）

| 対象 | 回数 | 分割後の扱い |
|---|---|---|
| `packages/api/pyproject.toml` | 3 | 依存層が無効化される |
| `packages/jetuse_shared/` | 1 | 同上 |
| `jetuse_core` / `service` / `fn` | **33** | **キャッシュが効く** |

37回中33回（89%）が82秒で済む。

### ビルドコンテキスト

`.dockerignore` が無く、リポジトリ全体（2.7GB / `runs` だけで223MB）が対象だった。
追加後の転送量は **7.58kB**。

## 等価性の検証（分割前後で同じイメージになるか）

比較対象: 分割前の Containerfile で作った `dev-sogawa-ee142e5` と、分割後の `layersplit`。

| 観点 | 結果 |
|---|---|
| パッケージ**名**の集合 | **79件すべて一致** |
| バージョン差 | 2件のみ（下記） |
| `import service.main / jetuse_core / fn` | 両方 OK |
| uvicorn 起動 | `Application startup complete.` |
| `GET /docs` | **HTTP 200** |
| `GET /openapi.json` | **HTTP 200** |
| `GET /api/chat/models` | **HTTP 200** |

### バージョン差2件は分割由来ではない

```
openai-agents  0.19.3 → 0.19.4
packaging      26.2   → 26.3
```

PyPI の公開日を確認したところ **`openai-agents 0.19.4` は 2026-08-05、`packaging 26.3` は
2026-08-04** のリリース。`pyproject` は `openai-agents>=0.17` のように `>=` 制約なので、
旧イメージ（2026-08-04 ビルド）と新イメージ（2026-08-05 ビルド）で解決結果が異なるのは
時間経過によるもの。**分割は依存解決のロジックを変えていない**（`[project] dependencies`
をそのまま `-r` で入れ、自パッケージは `--no-deps` で入れるため）。

## 実装

```dockerfile
# --- 変わりにくい層: 依存 ---
COPY packages/jetuse_shared /jetuse_shared
COPY packages/api/pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(...['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt uvicorn

# --- 変わりやすい層: アプリのコード ---
COPY packages/api/jetuse_core ./jetuse_core
...
RUN pip install --no-cache-dir --no-deps . && chmod +x entrypoint.sh
```

`jetuse-shared @ file:../jetuse_shared` は path 依存なので、`jetuse_shared` を先に COPY
してから依存を入れる順序でなければ解決できない。自パッケージは純 Python
（`[tool.setuptools] packages = [...]`）でコンパイルが無いため、`--no-deps` 側は軽い。

## 検証中に踏んだ落とし穴（記録）

`docker run -d -p ...` でポート公開すると応答しなかったが、**前景実行では正常に起動**していた
（`Application startup complete.` / `Uvicorn running on http://0.0.0.0:8000`）。
イメージの問題ではなく、この機の Docker Desktop がエミュレーション下でポート転送に失敗する
環境要因。**コンテナ内から HTTP を叩いて**決着させた。

## 実 OCI での動作確認（分割後イメージ）

ローカル docker 内だけでなく、**分割後のイメージを実際に jetuse:dev へ配備**して確認した
（Codex の指摘 review-9: 「ローカル検証だけでは変更後の成果物が実環境で動く裏づけにならない」）。

配備: `ops/dev-env-up.sh sogawa --apply` / 稼働イメージ `jetuse-dev-api:dev-sogawa-20ac5b1`
（`internal-dev` の HEAD を分割後 Containerfile でビルドしたもの）

```
Apply complete! Resources: 1 added, 1 changed, 1 destroyed.
```

| 経路 | 結果 |
|---|---|
| `GET /` | 200 |
| `GET /api/health` | 200 |
| `GET /api/chat/models` | 200 |
| `GET /api/capabilities` | 200 |
| `GET /api/demos`（内部固有・DB 経路） | **200** |
| `POST /api/chat/stream`（実推論） | `{"delta": "7"}` — `3+4` に正答・SSE 動作 |
| `POST /api/builder/sessions`（SP3・DB 書き込み） | 200 / `status=hearing` |

### 途中で ADB が停止していた（変更とは無関係）

確認中に `/api/demos` が 503、`dbchat` が `unavailable` になった。切り分けたところ
**`jetuse-dev-adb` が STOPPED** だった（ローカルからの直接接続も失敗した）。
分割とは無関係の環境要因。起動して `AVAILABLE` にしたところ即座に復旧した。

**依存層の無効化が正しく働くことも確認できた。** `public-dev` で作ったキャッシュは
`internal-dev` のビルドでは再利用されなかった（`packages/jetuse_shared/` が両ブランチで
異なるため）。ブランチをまたぐと依存層が正しく作り直される。

## CI への効果（未検証）

`release.yml` / `deploy-dev.yml` は ubuntu ネイティブで13〜14分 / 7〜11分。同じ構造なので
依存インストールが大半を占めるはずだが、**GitHub Actions は既定でレイヤキャッシュを保持しない**
ため、CI で恩恵を得るには `cache-from` / `cache-to`（GHA cache）の設定が別途要る。
本 PR には含めていない。

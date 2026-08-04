# E2E-4: コンテナエンジンのフォールバック（podman → docker）

対象: `ops/_container.sh`（新規）/ `ops/dev-env-up.sh` / `ops/deploy-hosted-agent.sh`
実 OCI（OCIR ord）に対して実施。モック不使用。

## なぜ必要だったか

Internal リリースの E2E を取ろうとして**配備が始まらなかった**。

```
$ ops/dev-env-up.sh sogawa
podman: command not found
```

`ops/dev-env-up.sh:44` と `ops/deploy-hosted-agent.sh:28` が `podman` を直書きしていた。
CLAUDE.md は「podman 5.6」を**確定事実**として載せているが、実際の開発機には
docker 29 しか入っていない。ドキュメントと実機の乖離がそのまま配備の停止になっていた。

build / push はどちらのエンジンでも同じサブコマンドで通るので、在る方を使う。

## 結果

### 解決ロジック（単体・6ケース）

| ケース | 期待 | 実測 |
|---|---|---|
| 両方ある | podman | PASS |
| docker のみ | **docker** | PASS |
| podman のみ | podman | PASS |
| どちらも無い | 落とす | PASS（`見つからない` / rc≠0） |
| `JETUSE_CONTAINER_ENGINE=docker` | docker（podman があっても） | PASS |
| 明示指定が PATH に無い | 落とす | PASS（`PATH に無い` / rc≠0） |

テストは PATH を偽物ディレクトリだけに差し替えて実施している。`/usr/bin` を混ぜると
ホストの podman/docker が残り「どちらも無い」ケースを作れない。

### 実機（docker で実際に build / push）

```
$ . ops/_container.sh && jetuse_container_engine
docker

$ docker build -f packages/api/Containerfile -t ord.ocir.io/<ns>/jetuse-dev-api:e2e-fallback-fbe9854 .
build rc=0     → 780MB

$ docker push ord.ocir.io/<ns>/jetuse-dev-api:e2e-fallback-fbe9854
push rc=0      → digest: sha256:<masked> size: 2829
```

**PASS。** podman が無い環境でも配備経路の build / push が通る。

## 副次的に直したもの

CLAUDE.md の「ツール: … podman 5.6」を実態に合わせた。確定事実として書いてある値が
実機と食い違うと、今回のように**その値を前提にした自動化が止まる**。

## 片付け

検証用に push した `e2e-fallback-*` タグのイメージは `jetuse-dev-api`（開発用リポジトリ）
に残る。開発用イメージは日常的に上書きされるため、個別削除はしない。

# E2E-6: build の platform 未固定で配備が壊れ、復旧できなくなった

対象: `ops/dev-env-up.sh` / `ops/deploy-hosted-agent.sh`
実 OCI（us-chicago-1 / Container Instance）で実際に踏んだ事象。

## 症状

Internal リリース点を配備しようとして apply が失敗した。

```
Error: Work Request error
Error Message: work request did not succeed, ... entity: containerinstance, action: CREATED.
Message: A container image provided is not compatible with the processor architecture
         of the shape selected for the container instance.
```

## 原因

配備スクリプトが `--platform` を指定していなかった。開発機は Apple Silicon なので
`docker build` が **arm64** イメージを作り、Container Instance の shape（x86）が弾いた。

```
$ docker image inspect ...:dev-sogawa-ee142e5 --format '{{.Os}}/{{.Architecture}}'
linux/arm64
```

## なぜ「落ちたら直せばいい」で済まないか

`image_url` の変更は Container Instance の**置換**になる。terraform は**旧インスタンスを
先に削除**してから新規作成するため、作成に失敗すると**環境が落ちたまま復旧できない**。

```
$ oci container-instances container-instance list --region us-chicago-1 ...
jetuse-sogawa-api   FAILED
jetuse-sogawa-api   DELETED     ← 旧インスタンス
```

実際に自分の dev 環境を落とした。

## なぜ今まで露見しなかったか

CI（`deploy-dev.yml`）は **ubuntu / x86 ランナー**で build するため、`--platform` 無しでも
正しい aarch を作る。**ローカル（Apple Silicon）経路だけが壊れていた**。
`podman` を直書きしていたこと（PR #139 で解消）と重なって、そもそもローカル配備が
到達していなかったため表に出ていなかった。

## 修正と、修正後の実測

`linux/amd64` を既定に固定し、`JETUSE_BUILD_PLATFORM` で上書き可能にした。

**修正前**
```
$ docker image inspect ...:dev-sogawa-ee142e5 --format '{{.Os}}/{{.Architecture}}'
linux/arm64                     ← Container Instance が弾いた
```

**修正後（同じスクリプト・同じコミット）**
```
== build & push ...:dev-sogawa-ee142e5 (engine=docker platform=linux/amd64)
...
Layer already exists            ← OCIR への push 成功

$ docker image inspect ...:dev-sogawa-ee142e5 --format '{{.Os}}/{{.Architecture}}'
linux/amd64                     ← 配備先 shape と一致
```

同一タグを上書きしているため、OCIR 上のイメージも amd64 に置き換わっている。

## テスト

シェルの実行ではなく**スクリプトの記述**を検査する（実 build は数十分かかるため）。

| 検査 | 内容 |
|---|---|
| build 行が存在する | 検査対象が消えて空振りしないこと |
| すべての build 行が `--platform` を渡す | 指定が消えていないこと |
| 既定が `linux/amd64` | 配備先 shape に合うこと |
| エンジンの直書きが無い | `ops/_container.sh` 経由に統一されていること |

## 残っている問題（未解決）

**Apple Silicon での amd64 クロスビルドが実用的でない。** QEMU エミュレーション下の
`pip install` が 40 分以上かかり、ローカルからの配備は現実的な時間で終わらない。
`--platform` の固定は**正しさ**の問題を解いたが、**実行時間**の問題は残る。

選択肢（未検討・要判断）:
- CI（`deploy-dev.yml`）に配備を寄せる（ubuntu x86 でネイティブビルド）
- `dispatch-remote` で x86 のインスタンス `dev` 上でビルドする
- ローカルは plan までにし、apply は CI に任せる

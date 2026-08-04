#!/usr/bin/env bash
# コンテナエンジンを解決する（podman 優先・docker フォールバック）。
#
# なぜ要るか（2026-08-04 の実害）:
#   ops/dev-env-up.sh と ops/deploy-hosted-agent.sh が podman を直書きしていた。
#   CLAUDE.md は「podman 5.6」を確定事実として載せているが、実際の開発機には
#   docker しか入っておらず、`podman: command not found` で配備が始まらなかった。
#   どちらのエンジンでも build/push は同じサブコマンドで通るので、在る方を使う。
#
# 使い方:
#   . "$(dirname "$0")/_container.sh"
#   CE=$(jetuse_container_engine)      # 見つからなければメッセージを出して exit 1
#   "$CE" build -f ... -t "$IMAGE" .
#
# 上書き: JETUSE_CONTAINER_ENGINE=docker のように明示指定できる。

jetuse_container_engine() {
  local want="${JETUSE_CONTAINER_ENGINE:-}"
  if [ -n "$want" ]; then
    command -v "$want" >/dev/null 2>&1 || {
      echo "JETUSE_CONTAINER_ENGINE=$want が PATH に無い" >&2; return 1; }
    printf '%s' "$want"; return 0
  fi
  local c
  for c in podman docker; do
    if command -v "$c" >/dev/null 2>&1; then printf '%s' "$c"; return 0; fi
  done
  echo "podman も docker も見つからない。どちらかを入れるか JETUSE_CONTAINER_ENGINE を指定してください。" >&2
  return 1
}

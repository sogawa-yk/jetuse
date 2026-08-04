# JetUse 単一コマンド入口（ローカル macOS が主開発環境）。詳細は CLAUDE.md / README を参照。
# venv パスは上書き可: make test PY=/path/to/.venv/bin
PY  ?= .venv/bin
DEV ?= $(USER)

.PHONY: help build test lint e2e deploy down clean

help:   ## コマンド一覧
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:[^#]*## /\t— /' | sort

build:  ## web(SPA) をビルド（api はコンテナ・deploy 時に build）
	npm --prefix packages/web run build

test:   ## web + api の単体テスト
	npm --prefix packages/web run test
	$(PY)/pytest packages/api/tests

lint:   ## web + api + infra の lint（CI と同じものを見る）
	npm --prefix packages/web run lint
	$(PY)/ruff check packages/api
	ops/check-infra.sh
	ops/check-branch-base.sh

e2e:    ## 実OCIへの E2E smoke（DB migrate 冪等）。scenario は tasks/<id>.md の「E2E シナリオ」参照
	$(PY)/python -m jetuse_core.migrate
	@echo "→ scenario 単位の E2E（実URL/実ADB）は make deploy DEV=<名> 後に tasks/<id>.md に従い実施"

deploy: ## 自分の dev 環境へデプロイ（terraform apply 含む）。例: make deploy DEV=alice
	ops/dev-env-up.sh $(DEV) --apply

down:   ## 自分の dev 環境を破棄（破壊的）。例: make down DEV=alice
	ops/dev-env-down.sh $(DEV) --yes

clean:  ## ビルド生成物を削除
	rm -rf packages/web/dist

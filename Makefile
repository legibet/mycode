GO_DIR := mycode-go
WEB_DIR := web
DIST_DIR := $(CURDIR)/dist
BIN := $(DIST_DIR)/mycode-go
WEB_EMBED_TAG := embedweb

.PHONY: fmt-go vet-go test-go test-go-race lint-go update-models-catalog web-install web-build web-check check build run web-dev clean

fmt-go:
	go -C $(GO_DIR) fmt ./...

vet-go:
	go -C $(GO_DIR) vet ./...

test-go:
	go -C $(GO_DIR) test ./...

test-go-race:
	go -C $(GO_DIR) test -race ./...

lint-go:
	cd $(GO_DIR) && golangci-lint run ./...

update-models-catalog:
	uv run --no-project python ./scripts/update_models_catalog.py

web-install:
	pnpm --dir $(WEB_DIR) install --frozen-lockfile

web-build: web-install
	pnpm --dir $(WEB_DIR) build
	./scripts/sync_web_dist.sh

web-check:
	pnpm --dir $(WEB_DIR) check
	pnpm --dir $(WEB_DIR) typecheck
	pnpm --dir $(WEB_DIR) test:run

check: vet-go test-go-race lint-go web-check

build: web-build
	mkdir -p $(DIST_DIR)
	go -C $(GO_DIR) build -tags $(WEB_EMBED_TAG) -o $(BIN) ./cmd/mycode-go

run:
	go -C $(GO_DIR) run ./cmd/mycode-go

web-dev:
	go -C $(GO_DIR) run ./cmd/mycode-go web --dev

clean:
	rm -rf $(DIST_DIR)
	rm -rf $(GO_DIR)/internal/server/webdist

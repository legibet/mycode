WEB_DIR := web
DIST_DIR := $(CURDIR)/dist
BIN := $(DIST_DIR)/mycode-go
WEB_EMBED_TAG := embedweb
WAILS_VERSION := v2.12.0
WAILS_BIN := $(shell GOWORK=off go env GOPATH)/bin/wails
WAILS_FRONTEND_DIST := frontend/dist
WAILS_FLAGS ?=
WAILS_DEV_FLAGS ?=

.DEFAULT_GOAL := help

.PHONY: help
help:
	@printf '%s\n' \
		'Targets:' \
		'  make dev          Start backend and frontend dev servers.' \
		'  make run-web      Run the Go web command with go run.' \
		'  make web-install  Install web dependencies.' \
		'  make web-dev      Start only the frontend dev server.' \
		'  make web-check    Run web lint, typecheck, and tests.' \
		'  make web-build    Build web assets and sync them for embedding.' \
		'  make fmt          Format Go code.' \
		'  make lint         Lint Go code.' \
		'  make test         Run Go tests.' \
		'  make check        Run all Go and web checks.' \
		'  make build        Build the embedded binary.' \
		'  make wails-install Install the Wails CLI.' \
		'  make wails-dev    Start the Wails desktop app.' \
		'  make wails-build  Build the Wails desktop app.' \
		'  make clean        Remove build outputs.'

.PHONY: dev
dev:
	@trap 'kill 0' INT TERM EXIT; \
	go run ./cmd/mycode-go web --dev & \
	pnpm --dir $(WEB_DIR) dev & \
	wait

.PHONY: run-web
run-web:
	go run ./cmd/mycode-go web

.PHONY: web-dev
web-dev:
	pnpm --dir $(WEB_DIR) dev

.PHONY: web-install
web-install:
	pnpm --dir $(WEB_DIR) install --frozen-lockfile

.PHONY: web-check
web-check:
	pnpm --dir $(WEB_DIR) check
	pnpm --dir $(WEB_DIR) typecheck
	pnpm --dir $(WEB_DIR) test:run

.PHONY: web-build
web-build: web-install
	pnpm --dir $(WEB_DIR) build
	./scripts/sync_web_dist.sh

.PHONY: fmt
fmt:
	golangci-lint fmt ./...

.PHONY: lint
lint:
	golangci-lint run ./...

.PHONY: test
test:
	go test ./...

.PHONY: check
check:
	go vet ./...
	go test -race ./...
	golangci-lint run ./...
	$(MAKE) web-check

.PHONY: build
build: web-build
	mkdir -p $(DIST_DIR)
	go build -tags $(WEB_EMBED_TAG) -o $(BIN) ./cmd/mycode-go

.PHONY: wails-install
wails-install:
	GOWORK=off go install github.com/wailsapp/wails/v2/cmd/wails@$(WAILS_VERSION)

.PHONY: wails-doctor
wails-doctor:
	$(WAILS_BIN) doctor

.PHONY: wails-dev
wails-dev:
	GOWORK=off $(WAILS_BIN) dev $(WAILS_DEV_FLAGS)

.PHONY: wails-frontend
wails-frontend:
	./scripts/build_wails_frontend.sh

.PHONY: wails-build
wails-build:
	./scripts/build_wails_app.sh $(WAILS_FLAGS)

.PHONY: wails-clean
wails-clean:
	rm -rf build/bin
	mkdir -p $(WAILS_FRONTEND_DIST)
	find $(WAILS_FRONTEND_DIST) -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +

.PHONY: clean
clean: wails-clean
	rm -rf $(DIST_DIR)
	rm -rf internal/server/webdist

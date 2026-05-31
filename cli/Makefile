BINARY     := migration-cli
MODULE     := github.com/mulleradriane/migration-cli
VERSION    := $(shell git describe --tags --always --dirty 2>/dev/null || echo "v1.0.0-dev")
COMMIT     := $(shell git rev-parse --short HEAD 2>/dev/null || echo "none")
BUILD_DATE := $(shell date -u +"%Y-%m-%dT%H:%M:%SZ")
LDFLAGS    := -ldflags "-X main.version=$(VERSION) -X main.commit=$(COMMIT) -X main.buildDate=$(BUILD_DATE) -s -w"

# Diretório de release
DIST_DIR   := dist

# Plataformas para cross-compile
PLATFORMS  := linux/amd64 linux/arm64 darwin/amd64 darwin/arm64

.PHONY: all build install clean test vet lint release help

## all: build padrão
all: build

## build: compila o binário para a plataforma atual
build:
	@echo "→ Compilando $(BINARY) $(VERSION)..."
	@go build $(LDFLAGS) -o $(BINARY) .
	@echo "✅ Binário: ./$(BINARY)"

## install: compila e instala no GOPATH/bin (ou /usr/local/bin se GOPATH não definido)
install: build
	@INSTALL_DIR=$${GOPATH:+$(GOPATH)/bin}; \
	INSTALL_DIR=$${INSTALL_DIR:-/usr/local/bin}; \
	echo "→ Instalando em $${INSTALL_DIR}/$(BINARY)..."; \
	mkdir -p $${INSTALL_DIR}; \
	cp $(BINARY) $${INSTALL_DIR}/$(BINARY); \
	echo "✅ Instalado: $${INSTALL_DIR}/$(BINARY)"

## release: cross-compila para linux/darwin amd64/arm64 e empacota em arquivos tar.gz
release: clean-dist
	@echo "→ Cross-compilando para múltiplas plataformas..."
	@mkdir -p $(DIST_DIR)
	@for platform in $(PLATFORMS); do \
		OS=$$(echo $$platform | cut -d'/' -f1); \
		ARCH=$$(echo $$platform | cut -d'/' -f2); \
		OUTPUT=$(DIST_DIR)/$(BINARY)-$(VERSION)-$$OS-$$ARCH; \
		echo "  → $$OS/$$ARCH..."; \
		GOOS=$$OS GOARCH=$$ARCH go build $(LDFLAGS) -o $$OUTPUT . || exit 1; \
		tar -czf $$OUTPUT.tar.gz -C $(DIST_DIR) $(BINARY)-$(VERSION)-$$OS-$$ARCH; \
		rm $$OUTPUT; \
		echo "  ✅ $(DIST_DIR)/$(BINARY)-$(VERSION)-$$OS-$$ARCH.tar.gz"; \
	done
	@echo ""
	@echo "✅ Release artifacts em ./$(DIST_DIR)/:"
	@ls -lh $(DIST_DIR)/

## test: executa os testes
test:
	@echo "→ Executando testes..."
	@go test -v ./...

## vet: executa go vet
vet:
	@echo "→ Executando go vet..."
	@go vet ./...
	@echo "✅ go vet OK"

## lint: executa golangci-lint (deve estar instalado)
lint:
	@if command -v golangci-lint >/dev/null 2>&1; then \
		echo "→ Executando golangci-lint..."; \
		golangci-lint run ./...; \
	else \
		echo "⚠️  golangci-lint não instalado. Instale: https://golangci-lint.run/usage/install/"; \
	fi

## tidy: executa go mod tidy
tidy:
	@echo "→ go mod tidy..."
	@go mod tidy
	@echo "✅ go.mod e go.sum atualizados"

## clean: remove binários locais
clean:
	@echo "→ Removendo binários..."
	@rm -f $(BINARY)
	@echo "✅ Limpo"

## clean-dist: remove o diretório de release
clean-dist:
	@rm -rf $(DIST_DIR)

## help: lista os targets disponíveis
help:
	@echo ""
	@echo "migration-cli — Makefile targets:"
	@echo ""
	@grep -E '^## ' Makefile | sed 's/## /  /' | column -t -s ':'
	@echo ""
	@echo "Variáveis:"
	@echo "  VERSION = $(VERSION)"
	@echo "  COMMIT  = $(COMMIT)"
	@echo ""

#!/usr/bin/env bash
# smoke_test.sh — valida que o migration-cli funciona end-to-end
# Cria buckets reais na conta AWS ativa, roda o pipeline completo e limpa tudo.
#
# Uso:
#   ./smoke_test.sh                    # roda todos os cenários
#   ./smoke_test.sh --keep-buckets     # não deleta os buckets ao final (para inspecionar)
#   ./smoke_test.sh --skip-terraform   # pula as etapas de terraform (só extract+generate)
#
# Requisitos:
#   - AWS configurado (aws sts get-caller-identity deve funcionar)
#   - ./migration-cli compilado (make build)
#   - terraform instalado (opcional, necessário sem --skip-terraform)

set -euo pipefail

# ── Configuração ──────────────────────────────────────────────────────────────
CLI="./migration-cli"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PREFIX="smktest"
OUTDIR="/tmp/migration-cli-smoke"
KEEP_BUCKETS=false
SKIP_TF=true   # padrão: pula terraform (evita precisar do backend S3 real)

# Parse args
for arg in "$@"; do
  case $arg in
    --keep-buckets)   KEEP_BUCKETS=true ;;
    --skip-terraform) SKIP_TF=true ;;
    --with-terraform) SKIP_TF=false ;;
  esac
done

# ── Cores ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

PASS=0; FAIL=0
declare -a FAILURES

ok()   { echo -e "  ${GREEN}✅ $*${RESET}"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}❌ $*${RESET}"; FAIL=$((FAIL + 1)); FAILURES+=("$*"); }
info() { echo -e "  ${CYAN}ℹ  $*${RESET}"; }
step() { echo -e "\n${BOLD}${CYAN}━━━ $* ━━━${RESET}"; }

# ── Cenários de teste ─────────────────────────────────────────────────────────
# Formato: "bucket_suffix|team|env|asset_category|config_fn"
# config_fn: nome de função que configura o bucket depois de criado
SCENARIOS=(
  "plain|clitest|dev|Logs|setup_plain"
  "lc|clitest|dev|Backup|setup_lc"
  "versioning|clitest|dev|Code|setup_versioning"
  "cors|clitest|dev|Productive data|setup_cors"
  "legacycat|clitest|dev|Development|setup_plain"   # categoria legada → normaliza para Code
)

BUCKETS=()
for scenario in "${SCENARIOS[@]}"; do
  suffix=$(cut -d'|' -f1 <<< "$scenario")
  BUCKETS+=("${PREFIX}-${suffix}-dev")
done

# ── Setup funções ─────────────────────────────────────────────────────────────
setup_plain() { : ; }  # bucket vazio, sem configuração extra

setup_lc() {
  local bucket=$1
  aws s3api put-bucket-lifecycle-configuration \
    --bucket "$bucket" \
    --lifecycle-configuration '{
      "Rules": [{
        "ID": "archive-old",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "Transitions": [
          {"Days": 30, "StorageClass": "STANDARD_IA"},
          {"Days": 90, "StorageClass": "GLACIER_IR"}
        ],
        "Expiration": {"Days": 365}
      }]
    }' 2>&1 | grep -v "^{" || true
}

setup_versioning() {
  local bucket=$1
  aws s3api put-bucket-versioning \
    --bucket "$bucket" \
    --versioning-configuration Status=Enabled
}

setup_cors() {
  local bucket=$1
  aws s3api put-bucket-cors \
    --bucket "$bucket" \
    --cors-configuration '{
      "CORSRules": [{
        "AllowedHeaders": ["*"],
        "AllowedMethods": ["GET","PUT"],
        "AllowedOrigins": ["https://app.example.com"],
        "MaxAgeSeconds": 3000
      }]
    }'
}

# ── Cleanup ────────────────────────────────────────────────────────────────────
cleanup_buckets() {
  if $KEEP_BUCKETS; then
    echo -e "\n${YELLOW}⚠  --keep-buckets: buckets mantidos, delete manualmente:${RESET}"
    for b in "${BUCKETS[@]}"; do echo "  aws s3api delete-bucket --bucket $b --region $REGION"; done
    return
  fi
  echo -e "\n${CYAN}Limpando buckets de teste...${RESET}"
  for b in "${BUCKETS[@]}"; do
    if aws s3api head-bucket --bucket "$b" 2>/dev/null; then
      aws s3api delete-bucket --bucket "$b" --region "$REGION" 2>/dev/null && echo "  ✓ $b" || echo "  ✗ $b (falhou)"
    fi
  done
}
trap cleanup_buckets EXIT

# ═════════════════════════════════════════════════════════════════════════════
echo -e "\n${BOLD}migration-cli — Smoke Test${RESET}"
echo -e "Conta AWS: $(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
echo -e "Região: $REGION  |  Prefixo: $PREFIX"
echo -e "Output: $OUTDIR"
[[ $SKIP_TF == true ]] && echo -e "${YELLOW}Modo: sem terraform (use --with-terraform para incluir)${RESET}"
rm -rf "$OUTDIR" && mkdir -p "$OUTDIR"

# ── [0] Pré-requisitos ─────────────────────────────────────────────────────────
step "0/6 Pré-requisitos"

if [[ ! -x "$CLI" ]]; then
  fail "migration-cli não encontrado — rode: make build"
  exit 1
fi
ok "migration-cli encontrado: $($CLI --version 2>&1 | head -1)"

if ! aws sts get-caller-identity &>/dev/null; then
  fail "AWS não autenticado — configure credenciais"
  exit 1
fi
ok "AWS autenticado: $(aws sts get-caller-identity --query Arn --output text)"

# ── [1] Criar buckets de teste ────────────────────────────────────────────────
step "1/6 Criando buckets de teste"

for scenario in "${SCENARIOS[@]}"; do
  IFS='|' read -r suffix team env asset_cat config_fn <<< "$scenario"
  bucket="${PREFIX}-${suffix}-dev"

  if aws s3api create-bucket --bucket "$bucket" --region "$REGION" &>/dev/null; then
    $config_fn "$bucket" &>/dev/null || true
    ok "Criado e configurado: $bucket ($config_fn)"
  else
    fail "Falha ao criar: $bucket"
  fi
done

# ── [2] Discover ──────────────────────────────────────────────────────────────
step "2/6 Discover"

CSV="$OUTDIR/test.csv"
{
  echo "bucket_name,team,env,asset_category,category,blockers"
  for scenario in "${SCENARIOS[@]}"; do
    IFS='|' read -r suffix team env asset_cat config_fn <<< "$scenario"
    echo "${PREFIX}-${suffix}-dev,${team},${env},${asset_cat},A,"
  done
} > "$CSV"

output=$($CLI s3 discover --csv "$CSV" 2>&1)
echo "$output"

total=$(echo "$output" | grep -oP 'Discovery Results \(\K\d+' || echo "0")
if [[ "$total" == "${#SCENARIOS[@]}" ]]; then
  ok "Discover: $total buckets encontrados"
else
  fail "Discover: esperava ${#SCENARIOS[@]} buckets, got $total"
fi

# legacycat deve ser REVIEW (categoria normalizada)
if echo "$output" | grep -q "smktest-legacycat-dev" && echo "$output" | grep -q "REVIEW"; then
  ok "Normalização de categoria: Development → REVIEW"
else
  fail "Normalização de categoria não funcionou"
fi

# ── [3] Preflight ─────────────────────────────────────────────────────────────
step "3/6 Preflight"

for scenario in "${SCENARIOS[@]}"; do
  IFS='|' read -r suffix team env asset_cat config_fn <<< "$scenario"
  bucket="${PREFIX}-${suffix}-dev"
  if $CLI s3 preflight --bucket "$bucket" &>/dev/null; then
    ok "Preflight OK: $bucket"
  else
    fail "Preflight BLOCK inesperado: $bucket"
  fi
done

# ── [4] Extract ───────────────────────────────────────────────────────────────
step "4/6 Extract"

for scenario in "${SCENARIOS[@]}"; do
  IFS='|' read -r suffix team env asset_cat config_fn <<< "$scenario"
  bucket="${PREFIX}-${suffix}-dev"
  outdir="$OUTDIR/$suffix"

  if $CLI s3 extract --bucket "$bucket" --output-dir "$outdir" &>/dev/null; then
    config="$outdir/.s3config.json"
    if [[ -f "$config" ]]; then
      ok "Extract OK: $bucket → .s3config.json"
    else
      fail "Extract: .s3config.json não gerado para $bucket"
    fi
  else
    fail "Extract falhou: $bucket"
  fi
done

# Verifica conteúdo específico dos extracts
lc_cfg="$OUTDIR/lc/.s3config.json"
if [[ -f "$lc_cfg" ]] && python3 -c "
import json, sys
cfg = json.load(open('$lc_cfg'))
rules = cfg.get('Lifecycle', [])
assert len(rules) == 1, f'esperava 1 regra, got {len(rules)}'
assert rules[0]['ID'] == 'archive-old', 'ID da regra errado'
" 2>/dev/null; then
  ok "Extract lifecycle: 1 regra encontrada com ID correto"
else
  fail "Extract lifecycle: regra não extraída corretamente"
fi

vers_cfg="$OUTDIR/versioning/.s3config.json"
if [[ -f "$vers_cfg" ]] && python3 -c "
import json
cfg = json.load(open('$vers_cfg'))
assert cfg.get('Versioning', {}).get('Status') == 'Enabled'
" 2>/dev/null; then
  ok "Extract versioning: Status=Enabled"
else
  fail "Extract versioning: status não detectado"
fi

cors_cfg="$OUTDIR/cors/.s3config.json"
if [[ -f "$cors_cfg" ]] && python3 -c "
import json
cfg = json.load(open('$cors_cfg'))
assert len(cfg.get('CORS', [])) == 1
" 2>/dev/null; then
  ok "Extract CORS: 1 regra encontrada"
else
  fail "Extract CORS: regra não detectada"
fi

# ── [5] Generate ──────────────────────────────────────────────────────────────
step "5/6 Generate"

for scenario in "${SCENARIOS[@]}"; do
  IFS='|' read -r suffix team env asset_cat config_fn <<< "$scenario"
  bucket="${PREFIX}-${suffix}-dev"
  outdir="$OUTDIR/$suffix"
  # normaliza asset_cat legado para o generate
  [[ "$asset_cat" == "Development" ]] && asset_cat="Code"

  if $CLI s3 generate \
      --bucket "$bucket" \
      --team "$team" \
      --env "$env" \
      --asset-cat "$asset_cat" \
      --output-dir "$outdir" &>/dev/null; then

    if [[ -f "$outdir/main.tf" && -f "$outdir/backend.tf" && -f "$outdir/CHANGES.md" ]]; then
      ok "Generate OK: $bucket → main.tf + backend.tf + CHANGES.md"
    else
      fail "Generate: arquivos não gerados para $bucket"
    fi
  else
    fail "Generate falhou: $bucket"
  fi
done

# Verifica conteúdo do main.tf do bucket com lifecycle
lc_main="$OUTDIR/lc/main.tf"
if grep -q "lifecycle_rules" "$lc_main" && grep -q "GLACIER_IR" "$lc_main"; then
  ok "main.tf lifecycle: regras HCL geradas corretamente"
else
  fail "main.tf lifecycle: regras HCL ausentes"
fi

if grep -q "ignore_changes" "$lc_main"; then
  ok "main.tf: ignore_changes presente (AES256)"
else
  fail "main.tf: ignore_changes ausente"
fi

# Verifica backend.tf
lc_backend="$OUTDIR/lc/backend.tf"
if grep -q 'key.*services/s3/smktest-lc-dev/dev' "$lc_backend"; then
  ok "backend.tf: key no padrão BP correto"
else
  fail "backend.tf: key fora do padrão"
fi

# Verifica CHANGES.md
if grep -q "LIFECYCLE_CUSTOM" "$OUTDIR/lc/CHANGES.md"; then
  ok "CHANGES.md: LIFECYCLE_CUSTOM documentado"
else
  fail "CHANGES.md: LIFECYCLE_CUSTOM ausente"
fi

if grep -q "CORS_CONFIGURED" "$OUTDIR/cors/CHANGES.md"; then
  ok "CHANGES.md: CORS_CONFIGURED documentado"
else
  fail "CHANGES.md: CORS_CONFIGURED ausente"
fi

# ── [6] Migrate dry-run ───────────────────────────────────────────────────────
step "6/6 Migrate --dry-run"

for scenario in "${SCENARIOS[@]}"; do
  IFS='|' read -r suffix team env asset_cat config_fn <<< "$scenario"
  bucket="${PREFIX}-${suffix}-dev"
  [[ "$asset_cat" == "Development" ]] && asset_cat="Code"

  output=$($CLI s3 migrate \
    --bucket "$bucket" \
    --team "$team" \
    --env "$env" \
    --asset-cat "$asset_cat" \
    --output-dir "$OUTDIR/migrate-$suffix" \
    --dry-run 2>&1)

  if echo "$output" | grep -q "DRY-RUN: pipeline concluído"; then
    ok "Migrate dry-run OK: $bucket"
  else
    fail "Migrate dry-run falhou: $bucket"
    info "Output: $(echo "$output" | tail -3)"
  fi
done

# ═════════════════════════════════════════════════════════════════════════════
echo -e "\n${BOLD}━━━ RESULTADO FINAL ━━━${RESET}"
echo -e "  ${GREEN}✅ PASS: $PASS${RESET}"
echo -e "  ${RED}❌ FAIL: $FAIL${RESET}"

if [[ $FAIL -gt 0 ]]; then
  echo -e "\n  Falhas:"
  for f in "${FAILURES[@]}"; do
    echo -e "    ${RED}→ $f${RESET}"
  done
  echo
  exit 1
fi

echo -e "\n  ${GREEN}${BOLD}Todos os testes passaram! ✅${RESET}\n"

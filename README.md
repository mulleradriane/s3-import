# S3 Migration Toolkit

Conjunto de scripts para migrar buckets S3 da AWS para o Terraform Blueprint (BP).
Suporta +800 buckets em ondas por ambiente (`dev` → `hml` → `prd`) e por produto.

---

## Pré-requisitos

| Ferramenta | Versão mínima | Instalação |
|---|---|---|
| Python | 3.9+ | `brew install python` / `apt install python3` |
| AWS CLI | v2 | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| Terraform | 1.3+ | `tfenv install` / download direto |
| git | qualquer | `brew install git` |

**Variáveis de ambiente obrigatórias:**

```bash
export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"
```

**Credenciais AWS** (via SSO ou chaves):

```bash
aws sso login --profile seu-perfil
# ou
export AWS_PROFILE=seu-perfil
```

---

## Instalação

```bash
# 1. Clone o repositório de scripts (ou copie os arquivos)
git clone <repo-dos-scripts> scripts3
cd scripts3

# 2. Confirme que os scripts estão presentes
ls -1 *.py
# s3.py  S3_migrate.py  plan_reviewer.py  s3_discovery.py
# s3_config_extractor.py  s3_main_tf_gen.py  check_sqs_kms_policies.py

# 3. Aplique o patch de correções críticas no S3_migrate.py (se ainda não aplicado)
patch S3_migrate.py < S3_migrate.patch

# 4. Valide o ambiente
python3 s3.py check
```

### Aplicando o patch no Cursor

Se preferir aplicar manualmente no Cursor em vez do patch:

1. Abra `S3_migrate.py` no Cursor
2. Consulte `MUDANCAS.md` — cada mudança tem o bloco ANTES / DEPOIS com contexto
3. Aplique também as mudanças nos outros arquivos documentados em `MUDANCAS.md`:
   - `s3_config_extractor.py` (Mudança 6 — cache TTL)
   - `s3_discovery.py` (Mudança 6c — pass-through do max_age_hours)
   - `s3_main_tf_gen.py` (Mudança 7 — logging prefix warning)
   - `check_sqs_kms_policies.py` (Mudança 5 — bug scan_mr)

---

## Fluxo recomendado (por onda)

```
check → discover → run --dry-run → review → run (approve)
```

### 1. Validar pré-requisitos

```bash
python3 s3.py check
```

Verifica: Python 3.9+, scripts presentes, AWS CLI, credenciais AWS, Terraform, GITLAB_TOKEN, git.

---

### 2. Discovery — classificar buckets antes de migrar

```bash
# Todos os buckets de dev
python3 s3.py discover --env dev --csv levantamento_completo_s3.xlsx

# Filtrar por produto
python3 s3.py discover --env hml --csv levantamento.csv --product lno

# Com extração de configs da AWS (necessário se configs não estiverem em cache)
python3 s3.py discover --env hml --csv levantamento.csv --product lno --extract
```

O discover também mostra uma análise de nomenclatura — identifica buckets fora do padrão
`ecs-{produto}-{logico}-{env}` e sinaliza divergências (ex: team tag não bate com o nome).

**Categorias de nomenclatura detectadas:**

| Categoria | Padrão | Exemplo |
|---|---|---|
| `padrao_ecs` | `ecs-{produto}-{logico}-{env}` | `ecs-lno-payments-hml` |
| `legado_s3` | `s3-{time}-*` ou `*.ecsbr.net` | `s3-ecred-data` |
| `ecs_outro_time` | `ecs-*` com team tag diferente | `ecs-infra-logs-prd` (team=ops) |
| `legado_nome_direto` | nome direto do produto | `platform-api-prd` |
| `legado_outros` | `ecsops-*`, `datadog-*`, `logs-*` | `ecsops-backup` |
| `legado_accountid` | `{accountId}-*` | `387979423286-artifacts` |
| `unknown` | sem tag Team, sem padrão | — |

---

### 3. Dry-run — gera arquivos e plans localmente

```bash
# Dry-run completo para um produto em HML
python3 s3.py run --env hml --csv levantamento.csv --product lno --dry-run

# Dry-run para um bucket específico
python3 s3.py run --env dev --csv levantamento.csv --bucket ecs-lno-payments-dev --dry-run

# Re-extrair configs se o cache tiver mais de 24h
python3 s3.py run --env hml --csv levantamento.csv --product lno --dry-run --max-cache-age 24
```

O dry-run:
- Extrai configs de todos os buckets via AWS API
- Gera `main.tf` e `CHANGES.md` para cada bucket
- Executa `terraform plan` e salva `plan_output.json`
- **Não** abre MRs, não faz push, não move state

---

### 4. Revisar plans

```bash
# Review automático de todos os plans gerados
python3 s3.py review

# Filtrar por bucket
python3 s3.py review --bucket ecs-lno-payments-hml

# Salvar relatório em arquivo
python3 s3.py review --output plan_review.md

# CI: falha se houver bloqueados
python3 s3.py review --fail-on-blocked
```

O review classifica cada operação do plan como:

| Nível | Cor | Significado |
|---|---|---|
| `BLOCKED` | 🔴 | Operação destrutiva inesperada — MR não será aberta |
| `REVIEW` | 🟡 | Mudança relevante (lifecycle, policy) — pede confirmação individual |
| `INFO` | 🔵 | Mudança de baixo risco — avisa mas aprova |
| `SAFE` | ✅ | Sem mudanças críticas |

---

### 5. Abrir MRs

```bash
# Aprovar e abrir MRs (requer --ticket)
python3 s3.py run --env hml --csv levantamento.csv --product lno --ticket SRE-1234

# Processar PRD (requer --confirm-prd)
python3 s3.py run --env prd --csv levantamento.csv --product lno --confirm-prd --ticket SRE-5678

# 40+ buckets sem confirmação interativa por bucket
python3 s3.py run --env prd --csv levantamento.csv --product lno \
    --confirm-prd --auto-confirm --ticket SRE-5678

# Excluir buckets específicos do lote de MR
python3 s3.py run --env hml --csv levantamento.csv --product lno --ticket SRE-1234 \
    --exclude ecs-lno-legacy-hml ecs-lno-problematic-hml
```

**Confirmação interativa antes das MRs** (sem `--auto-confirm`):

O script exibe um resumo de todos os plans e pergunta:
- Buckets **BLOCKED** → excluídos automaticamente (sem pergunta)
- Buckets **REVIEW** → pergunta individual com contexto do plan
- Buckets **CLEAN** → pergunta única para todos; você pode excluir por número:
  ```
  [1] ✅ ecs-lno-payments-hml
  [2] ✅ ecs-lno-documents-hml
  [3] ✅ ecs-lno-archive-hml

  Abrir MR para os 3 bucket(s) limpos? [s/N/excluir N,...]
  > excluir 2
  # abrirá MR para 1 e 3, pula o 2
  ```

---

### 6. Verificar status

```bash
python3 s3.py status
python3 s3.py status --output-dir ./mr_output
```

Mostra: quantos buckets processados, plans executados, erros de import/plan,
e o review summary se disponível.

---

## Referência de flags

### `run` — flags completas

| Flag | Padrão | Descrição |
|---|---|---|
| `--csv` | — | **Obrigatório.** Arquivo de levantamento |
| `--env` | — | **Obrigatório.** `dev`, `hml` ou `prd` |
| `--ticket` | — | Obrigatório sem `--dry-run` (ex: `SRE-1234`) |
| `--product` | — | Filtro por produto/time |
| `--bucket` | — | Processar apenas este bucket |
| `--dry-run` | false | Não abre MRs, não faz push |
| `--confirm-prd` | false | Confirma processamento de PRD |
| `--auto-confirm` | false | Pula confirmação interativa (modo lote) |
| `--exclude` | — | Buckets a excluir do lote de MR |
| `--one-mr-per-bucket` | false | Uma MR por bucket (padrão: uma por repo de time) |
| `--max-cache-age HORAS` | sem limite | Re-extrai configs se cache > N horas |
| `--parallel N` | 5 | Workers paralelos para extração |
| `--output-dir DIR` | `./mr_output` | Pasta de saída |
| `--repos-dir DIR` | `./repos` | Pasta de clones dos repos Terraform |

### Produtos conhecidos (`--product`)

```
antifraude  auth        b2b         chatbot     collection  core
crawler     cross       ctools      dataops     ecred       engineering
ewallet     fraudtools  gac         ia          id          infra
insurance   ipaas       lno         mobile      monitoring  nogordio
observability ops       partnerportal platform   premium     score
seguros     serasabox   sharedservices splunk    staffengineering web
```

---

## Estratégia de ondas

```
Onda 1 — DEV    (feito)   ~100 buckets, todos os produtos
Onda 2 — HML    (agora)   ~100 buckets, começar por produto menor (ex: lno)
Onda 3 — PRD    (depois)  após HML estável, com --confirm-prd e ticket aberto
```

**Ordem recomendada por produto em HML:**

1. Produtos menores primeiro (< 5 buckets) para calibrar o processo
2. Depois produtos maiores (lno, ecred, core) onde o impacto de erro é maior
3. PRD apenas após validar o template final em HML

---

## Saídas geradas

```
mr_output/
  ecs-lno-payments-hml/       # pasta por bucket
    main.tf                   # Terraform gerado
    CHANGES.md                # o que a BP vai mudar (lifecycle, logging, etc.)
    plan_output.json          # saída do terraform plan -json
    _import_commands.sh       # comandos de import (se necessário)
    _import_error.txt         # erro de import (se falhou)
    _plan_error.txt           # erro de plan (se falhou)
  plan_review.md              # relatório consolidado do review
  discovery_report.md         # relatório do discover
```

---

## Solução de problemas

### Import falhou (`_import_error.txt`)

```bash
# Ver o erro
cat mr_output/ecs-lno-bucket-hml/_import_error.txt

# Importar manualmente e re-executar
cd repos/ecs-lno-default-aws-terraform/services/s3/bucket/hml
terraform import aws_s3_bucket.main ecs-lno-bucket-hml
cd ../../../../..
python3 s3.py run --env hml --csv lev.csv --bucket ecs-lno-bucket-hml --ticket SRE-1234
```

### DESTROY inesperado no plan

O script bloqueia a MR automaticamente. Para investigar:

```bash
python3 s3.py review --bucket ecs-lno-bucket-hml
cat mr_output/ecs-lno-bucket-hml/plan_output.json | python3 -m json.tool | grep -A5 "destroy"
```

### Re-extrair configs (cache expirado)

```bash
python3 s3.py run --env hml --csv lev.csv --product lno --dry-run --max-cache-age 0
# max-cache-age 0 força re-extração de todos
```

### Excluir bucket problemático do lote

```bash
# Via flag
python3 s3.py run --env hml --csv lev.csv --product lno --ticket SRE-1234 \
    --exclude ecs-lno-problema-hml

# Ou durante a confirmação interativa, na etapa de buckets limpos:
# > excluir 3     (exclui o bucket de número 3 da lista)
```

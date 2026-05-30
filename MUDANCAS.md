# Mudanças a aplicar no repositório

> **Gerado em:** 2026-05-30
> **Sessão:** correções críticas (P1-A, import failure, destroy) + HML readiness (cache TTL, logging prefix, auto-confirm)

## Como aplicar

### Opção 1 — patch (recomendado)
```bash
cd <raiz-do-repo-onde-estao-os-scripts>
patch -p0 < S3_migrate.patch
```

### Opção 2 — manualmente no Cursor
Use as seções abaixo: cada bloco tem o **contexto** (linhas ao redor), o **ANTES** e o **DEPOIS**.

---

## Arquivo: `S3_migrate.py`

### Mudança 1 — Import failure bloqueia MR
**Contexto:** função `fase3_terraform_plan`, logo após o bloco que verifica state legado.

```python
# ANTES:
        if import_sh.exists() and not already_imported:
            plog(f"     → rodando terraform import...")
            ok_import, out_import = run(
                "bash _import_commands.sh 2>&1",
                cwd=str(bucket_dir)
            )
            already_managed = any(x in out_import for x in [
                'Resource already managed', 'already exists',
                'already managed by Terraform', 'Cannot import'
            ])
            if already_managed:
                plog(f"     ⏭  Recursos já no state")
            elif not ok_import:
                plog(f"     ⚠️  Import com aviso: {out_import[:120]}", 'warning')
            else:
                plog(f"     ✅ Import concluído")
        elif not import_sh.exists():
            plog(f"     ⏭  Sem _import_commands.sh — state existente")

# DEPOIS:
        import_failed = False
        if import_sh.exists() and not already_imported:
            plog(f"     → rodando terraform import...")
            ok_import, out_import = run(
                "bash _import_commands.sh 2>&1",
                cwd=str(bucket_dir)
            )
            already_managed = any(x in out_import for x in [
                'Resource already managed', 'already exists',
                'already managed by Terraform', 'Cannot import'
            ])
            if already_managed:
                plog(f"     ⏭  Recursos já no state")
            elif not ok_import:
                import_failed = True
                plog(f"     ❌ Import falhou — MR não será aberta", 'error')
                for line in out_import.split('\n'):
                    if line.strip():
                        plog(f"        {line.strip()}", 'error')
                error_file = bucket_dir / "_import_error.txt"
                error_file.write_text(out_import)
                plog(f"     📄 Output completo: {error_file}", 'warning')
            else:
                plog(f"     ✅ Import concluído")
        elif not import_sh.exists():
            plog(f"     ⏭  Sem _import_commands.sh — state existente")
```

---

### Mudança 2 — Adicionar import_failed à lista de issues
**Contexto:** logo após o `terraform plan` ser rodado, onde começa `issues = []`.

```python
# ANTES:
        issues = []
        # Detecta operações perigosas

# DEPOIS:
        issues = []
        if import_failed:
            issues.append('IMPORT_FALHOU — verificar _import_error.txt e importar manualmente')
        # Detecta operações perigosas
```

---

### Mudança 3 — Detectar QUALQUER destroy inesperado (não só 3 recursos)
**Contexto:** dentro de `fase3_terraform_plan`, logo após `plan_lines = out_plan.split('\n')`.

```python
# ANTES:
        plan_lines = out_plan.split('\n')
        for line in plan_lines:
            s = line.strip()
            # Detecta destroy de recursos principais — formato: "# X will be destroyed"
            if 'will be destroyed' in s or 'will be replaced' in s:
                resource_name = s.replace('# ', '').replace(' will be destroyed', '').replace(' will be replaced', '').strip()
                # Só bloqueia recursos principais — não policies SQS internas do módulo
                SAFE_DESTROYS = [
                    'aws_sqs_queue_policy',   # renomeação de key no state — normal
                    'aws_s3_bucket_metric',   # só bloqueia se enable_bucket_metric ausente
                    'time_static',            # usado pela BP para tag AppliedAt — recria no apply
                    'time_rotating',          # idem
                ]
                is_safe = any(sd in resource_name for sd in SAFE_DESTROYS)
                if not is_safe and any(r in resource_name for r in [
                    'aws_s3_bucket.main',
                    'aws_s3_bucket_policy',
                    'aws_s3_bucket_replication',
                ]):
                    issues.append(f'DESTROY_RECURSO_PRINCIPAL: {resource_name[:80]}')
                    break

# DEPOIS:
        plan_lines = out_plan.split('\n')
        # Destroys conhecidos como seguros (renomeação de chave no state, recursos recriados pela BP, etc.)
        SAFE_DESTROYS = [
            'aws_sqs_queue_policy',        # renomeação de key no state — normal
            'aws_s3_bucket_metric',        # desativado quando enable_bucket_metric=false
            'time_static',                 # BP recria — tag AppliedAt
            'time_rotating',               # idem
            'aws_s3_bucket_acl',           # substituído por ownership_controls na BP
            'random_',                     # recursos auxiliares internos do módulo
        ]
        for line in plan_lines:
            s = line.strip()
            # Detecta destroy — formato: "# X will be destroyed" ou "# X will be replaced"
            if 'will be destroyed' in s or 'will be replaced' in s:
                resource_name = (s.replace('# ', '')
                                  .replace(' will be destroyed', '')
                                  .replace(' will be replaced', '')
                                  .strip())
                if not any(sd in resource_name for sd in SAFE_DESTROYS):
                    issues.append(f'DESTROY_INESPERADO: {resource_name[:80]}')
                    plog(f"     ❌ Destroy inesperado: {resource_name[:80]}", 'error')
```

---

### Mudança 4 — Corrigir scope de bucket_mr_flags (P1-A)
**Três sub-mudanças no mesmo arquivo:**

#### 4a — Assinatura da função fase4_mrs
```python
# ANTES:
def fase4_mrs(bucket_paths, plan_results, args, token):

# DEPOIS:
def fase4_mrs(bucket_paths, plan_results, args, token, bucket_mr_flags=None):
```

#### 4b — Dentro de fase4_mrs, verificação do diff semântico
```python
# ANTES:
            # Verifica também o flag do diff semântico
            sem_diff = bucket_mr_flags if "bucket_mr_flags" in dir() else {}

# DEPOIS:
            # Verifica também o flag do diff semântico (passado como parâmetro)
            sem_diff = bucket_mr_flags or {}
```

#### 4c — Chamada de fase4_mrs em main()
```python
# ANTES:
        mrs = fase4_mrs(bucket_paths, plan_results, args, token)

# DEPOIS:
        mrs = fase4_mrs(bucket_paths, plan_results, args, token, bucket_mr_flags=bucket_mr_flags)
```

---

## Arquivo: `check_sqs_kms_policies.py`

### Mudança 5 — Corrigir bug scan_mr or True (P2-B)
**Contexto:** função `main()`, após `ap.parse_args()`.

```python
# ANTES:
    configs_dir = None if args.no_configs else args.configs_dir
    # Por padrão: s3config + main.tf em mr_output (dedupe por queue_arn)
    scan_mr = args.scan_mr_output or True

# DEPOIS:
    configs_dir = None if args.no_configs else args.configs_dir
    scan_mr = args.scan_mr_output
```

---

## Arquivo: `s3_config_extractor.py`

### Mudança 6 — Cache TTL: re-extrair se config estiver velha

**Contexto:** função `extract_bucket`, logo após a abertura do arquivo de cache.

```python
# ANTES:
def extract_bucket(bucket_name, output_dir, force=False):
    """Extrai config de um bucket e salva em arquivo."""
    out_file = Path(output_dir) / f"{bucket_name}.s3config.json"

    # Skip se já extraído (idempotente)
    if out_file.exists() and not force:
        try:
            existing = json.loads(out_file.read_text())
            if existing.get('_bucket') == bucket_name:
                return 'CACHED', existing
        except:
            pass

# DEPOIS:
def extract_bucket(bucket_name, output_dir, force=False, max_age_hours=None):
    """Extrai config de um bucket e salva em arquivo."""
    out_file = Path(output_dir) / f"{bucket_name}.s3config.json"

    # Skip se já extraído e dentro do TTL
    if out_file.exists() and not force:
        try:
            existing = json.loads(out_file.read_text())
            if existing.get('_bucket') == bucket_name:
                if max_age_hours is not None:
                    from datetime import datetime, timezone
                    extracted_at = existing.get('_extracted_at', '')
                    if extracted_at:
                        age = datetime.now(timezone.utc) - datetime.fromisoformat(
                            extracted_at.replace('Z', '+00:00')
                        )
                        if age.total_seconds() / 3600 > max_age_hours:
                            print(f"  ⟳  {bucket_name} — cache com {age.seconds//3600}h "
                                  f"(> {max_age_hours}h) — re-extraindo")
                        else:
                            return 'CACHED', existing
                    else:
                        return 'CACHED', existing
                else:
                    return 'CACHED', existing
        except:
            pass
```

**Também adicionar `--max-cache-age` no CLI standalone (ao final da função `main` de `s3_config_extractor.py`):**
```python
# Dentro do bloco if __name__ == '__main__': / parser.add_argument section
# ADICIONAR após o --force existente:
parser.add_argument('--max-cache-age', type=int, default=None, metavar='HORAS',
                    help='Re-extrai se cache tiver mais de N horas (padrão: sem limite)')
# E passar para extract_bucket:
# ANTES:
futures = {ex.submit(extract_bucket, b, args.output_dir, args.force): b
# DEPOIS:
futures = {ex.submit(extract_bucket, b, args.output_dir, args.force, args.max_cache_age): b
```

**E no `s3_discovery.py`, passar o parâmetro (função `discover_one`):**
```python
# ANTES:
status, cfg = extract_bucket(bucket, configs_dir, force=force_extract)

# DEPOIS:
status, cfg = extract_bucket(bucket, configs_dir, force=force_extract,
                             max_age_hours=getattr(args, 'max_cache_age', None))
```

**E adicionar o argparse em `s3_discovery.py`:**
```python
# Após o --force-extract existente:
ap.add_argument('--max-cache-age', type=int, default=None, metavar='HORAS',
                help='Re-extrai config se cache tiver mais de N horas')
```

---

## Arquivo: `s3_main_tf_gen.py`

### Mudança 7 — Logging prefix: mostrar o prefix atual que a BP vai trocar

**Contexto:** função `gen_changes_md`, logo no início onde `log_target` é obtido.

```python
# ANTES (linha ~943):
    log_target, _ = get_logging_info(cfg)

# DEPOIS:
    log_target, log_prefix = get_logging_info(cfg)
```

**Depois, dentro da tabela de logging (já existente, linha ~1184):**
```python
# ANTES:
    if log_target and log_target != log_bucket:
        lines.append(
            f"| `logging` | `{log_target}` | Destino atual: `{log_target}` → BP vai usar: `{log_bucket}` | 🟠 |")
    else:
        lines.append(
            f"| `logging` | padrão | "
            f"BP gerencia logging → `{log_bucket}`; plan típico adiciona prefixo particionado | 🟡 |")

# DEPOIS:
    bp_log_prefix = f"{bucket_name}/"  # prefixo padrão que a BP aplica (partitioned)
    if log_target and log_target != log_bucket:
        lines.append(
            f"| `logging` | `{log_target}` | Destino atual: `{log_target}` → BP vai usar: `{log_bucket}` | 🟠 |")
    else:
        prefix_note = ""
        if log_prefix and log_prefix != bp_log_prefix:
            prefix_note = (f" Prefix atual: `{log_prefix}` → BP vai usar `{bp_log_prefix}` "
                           f"(partitioned). **Queries Athena/CloudWatch que usam o prefix antigo "
                           f"precisarão ser atualizadas.**")
        lines.append(
            f"| `logging` | padrão | "
            f"BP gerencia logging → `{log_bucket}`; plan típico adiciona prefixo particionado.{prefix_note} | 🟡 |")
```

**E no checklist (linha ~1324), adicionar aviso de prefix:**
```python
# Após o critical_item de logging existente, ADICIONAR:
    if log_prefix and log_prefix != bp_log_prefix:
        critical_items.append(
            f"**Logging prefix alterado:** `{log_prefix}` → `{bp_log_prefix}` — "
            f"confirmar que queries Athena/CloudWatch/S3 Select que filtram por esse "
            f"prefix foram atualizadas"
        )
```

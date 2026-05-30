#!/usr/bin/env python3
"""
s3_migrate.py — Orquestrador único de migração de buckets S3

Uso:
  python3 s3_migrate.py --csv levantamento_completo.csv --env dev --dry-run
  python3 s3_migrate.py --csv levantamento_completo.csv --env dev --approve
  python3 s3_migrate.py --csv levantamento_completo.csv --env hml --approve
  python3 s3_migrate.py --csv levantamento_completo.csv --env prd --confirm-prd --approve
  python3 s3_migrate.py --csv ... --env dev --team ecred --approve
  python3 s3_migrate.py --csv ... --env dev --bucket ecs-ecred-consumer-file-dev --approve

Flags:
  --env              dev | hml | prd | all
  --confirm-prd      obrigatório para incluir prd
  --approve          executa de verdade (sem isso é sempre dry-run)
  --skip-no-owner    ignora buckets sem team tag (padrão: True)
  --team             filtra por time específico
  --bucket           processa apenas um bucket
  --repos-dir        diretório base para clonar/encontrar repos (padrão: ./repos)
  --lifecycle-dir    diretório para JSONs de lifecycle (padrão: ./lifecycles)
  --output-dir       diretório para arquivos gerados (padrão: ./mr_output)
  --parallel         workers paralelos para extração de lifecycle (padrão: 5)
  --gitlab-url       URL base do GitLab (padrão: https://gitlab.ecsbr.net)
  --gitlab-token     token GitLab (ou env GITLAB_TOKEN)
  --state-bucket     bucket S3 do tfstate (padrão: 387979423286-tfstate)
  --state-region     região do tfstate (padrão: us-east-1)
"""

import argparse, csv, json, logging, os, re, shutil, subprocess, sys
from pathlib import Path as _Path
# Módulos de extração e geração (devem estar no mesmo diretório)
try:
    from s3_config_extractor import extract_bucket, load_config, compute_semantic_diff, format_diff_report, needs_mr_from_diff
    from s3_main_tf_gen import gen_all_files
    HAS_FULL_EXTRACTOR = True
except ImportError:
    HAS_FULL_EXTRACTOR = False
import threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ── Constantes ─────────────────────────────────────────────────────────────────
BP_SOURCE       = "git::https://gitlab.ecsbr.net/ecs/engineering/ecs-engineering-terraform-blueprint-aws-s3.git//default?ref=2"
LOGGING_BUCKET  = "ecs-387979423286-logging-s3"
DEFAULT_STATE_BUCKET = "387979423286-tfstate"
DEFAULT_STATE_REGION = "us-east-1"
DEFAULT_GITLAB_URL   = "https://gitlab.ecsbr.net"

REPOS_EXISTENTES = {
    'antifraude','auth','chatbot','collection','core','crawler','cross',
    'ctools','dataops','ecred','engineering','ewallet','fraudtools',
    'gac','id','infra','insurance','ipaas','lno','martech','nogordio',
    'observability','partnerportal','platform','score','serasapass'
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)
_print_lock = threading.Lock()

def plog(msg, level='info'):
    with _print_lock:
        getattr(log, level)(msg)

# ── Helpers de shell ───────────────────────────────────────────────────────────
def run(cmd, cwd=None, capture=True, dry_run=False, env=None):
    """Executa comando. Em dry_run só loga."""
    if dry_run:
        plog(f"  [DRY-RUN] {cmd}")
        return True, ''
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=capture, text=True,
            env={**os.environ, **(env or {})}
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, result.stdout.strip()
    except Exception as e:
        return False, str(e)

def check_tool(tool):
    return shutil.which(tool) is not None


def _cleanup_local_terraform_artifacts(module_dir: Path):
    """Remove artefatos de validate local antes do commit."""
    tf = module_dir / '.terraform'
    if tf.is_dir():
        shutil.rmtree(tf)
    lock = module_dir / '.terraform.lock.hcl'
    if lock.is_file():
        lock.unlink()


def _should_stage_s3_file(file_path: Path, module_root: Path) -> bool:
    if not file_path.is_file():
        return False
    if '.terraform' in file_path.parts or '__pycache__' in file_path.parts:
        return False
    if file_path.name.startswith('.') or file_path.name.startswith('_'):
        return False
    # policy.json só em files/ quando a cópia canônica existe
    if (
        file_path.name == 'policy.json'
        and file_path.parent.resolve() == module_root.resolve()
        and (module_root / 'files' / 'policy.json').is_file()
    ):
        return False
    return True


def git_add_s3_module(repo_path, rel, dry_run=False):
    """
    Stage do módulo S3 com git add -f (contorna .gitignore amplo, ex.: 'logs').
    Ignora .terraform/, dotfiles e arquivos com prefixo _ (_import_commands.sh).
    """
    base = Path(repo_path)
    root = base / rel
    if not root.is_dir():
        return run(f"git add -f '{rel}' 2>&1", cwd=repo_path, dry_run=dry_run)[0]

    _cleanup_local_terraform_artifacts(root)
    count = 0
    for item in sorted(root.rglob('*')):
        if not _should_stage_s3_file(item, root):
            continue
        rel_item = item.relative_to(base).as_posix()
        ok, out = run(f"git add -f '{rel_item}' 2>&1", cwd=repo_path, dry_run=dry_run)
        if ok:
            count += 1
        elif out:
            plog(f"    ⚠️  git add -f falhou para {rel_item}: {out[:120]}", 'warning')
    if count == 0:
        plog(f"    ⚠️  Nenhum arquivo elegível para stage em {rel}", 'warning')
    else:
        plog(f"    → {count} arquivo(s) no stage (git add -f; sem .terraform/_)")
    return count > 0


# ── Parser de CSV ──────────────────────────────────────────────────────────────
def load_csv(csv_path):
    """
    Lê CSV com detecção automática de encoding e separador.
    Suporta: utf-8, utf-8-bom, latin-1, cp1252 (Excel Brasil).
    Suporta separador ; ou , detectado automaticamente.
    """
    # Tenta encodings em ordem de prioridade
    encodings = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
    raw = None
    used_encoding = None

    for enc in encodings:
        try:
            with open(csv_path, encoding=enc, newline='') as f:
                raw = f.read()
            used_encoding = enc
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if raw is None:
        # Último recurso: lê em bytes e decodifica ignorando erros
        with open(csv_path, 'rb') as f:
            raw = f.read().decode('latin-1', errors='replace')
        used_encoding = 'latin-1 (fallback)'

    plog(f"  CSV encoding detectado: {used_encoding}")

    # Remove BOM se presente
    raw = raw.lstrip('\ufeff')

    # Detecta separador pela primeira linha
    first_line = raw.split('\n')[0] if raw else ''
    sep = ';' if first_line.count(';') > first_line.count(',') else ','
    plog(f"  CSV separador detectado: '{sep}'")

    reader = csv.DictReader(raw.splitlines(), delimiter=sep)
    rows = list(reader)

    # Limpa espaços extras nos valores (comum em exports do Excel)
    cleaned = []
    for row in rows:
        cleaned.append({k.strip(): (v.strip() if isinstance(v, str) else v)
                        for k, v in row.items() if k is not None})
    return cleaned

def coerce_bool(val):
    return str(val).strip().lower() in ('true','1','yes','sim')

# ── Extrai logical_name ────────────────────────────────────────────────────────
def extract_logical(bucket_name, team, env):
    prefix = f"ecs-{team}-"
    suffix = f"-{env}"
    if bucket_name.startswith(prefix) and bucket_name.endswith(suffix):
        return bucket_name[len(prefix):-len(suffix)], True
    return re.sub(r'^ecs-', '', bucket_name), False

# ── Filtra buckets a processar ─────────────────────────────────────────────────
def filter_buckets(rows, args):
    envs = []
    if args.env == 'all':
        envs = ['dev', 'hml']
        if args.confirm_prd:
            envs.append('prd')
        else:
            plog("PRD excluído — use --confirm-prd para incluir produção.", 'warning')
    elif args.env == 'prd':
        if not args.confirm_prd:
            log.error("Para processar prd use --env prd --confirm-prd")
            sys.exit(1)
        envs = ['prd']
    else:
        envs = [args.env]

    skipped = []
    selected = []

    for r in rows:
        bucket   = r.get('bucket_name', '').strip()
        team     = r.get('team', r.get('tags_team', '')).strip()
        env      = r.get('env', r.get('tags_env', '')).strip()
        category = r.get('category', '').strip()
        blockers = r.get('blockers', '').strip()

        # Filtra env
        if env not in envs:
            continue

        # Filtra bucket/team específico
        if args.bucket and bucket != args.bucket:
            continue
        if args.team and team != args.team:
            continue

        # Ignora sem owner
        if args.skip_no_owner and (not team or team in ('nan','None','')):
            skipped.append({**r, 'skip_reason': 'SEM_TEAM_TAG'})
            continue

        # Ignora D sem team
        if category == 'D' and (not team or team in ('nan','None','')):
            skipped.append({**r, 'skip_reason': 'CAT_D_SEM_OWNER'})
            continue

        # Ignora blockers
        if blockers and blockers.strip():
            blocker_list = [b.strip() for b in blockers.split('|') if b.strip()]
            if blocker_list:
                # --skip-blockers: ignora qualquer blocker silenciosamente
                if getattr(args, 'skip_blockers', False):
                    skipped.append({**r, 'skip_reason': f"BLOCKER_SKIPPED: {';'.join(blocker_list)}"})
                    continue
                # Comportamento padrão: ignora só os críticos não-owner
                skip_list = [b for b in blocker_list
                             if 'SEM_TEAM_TAG' not in b
                             and 'REPO_DESTINO_NAO_EXISTE' not in b]
                if skip_list:
                    skipped.append({**r, 'skip_reason': f"BLOCKER: {';'.join(skip_list)}"})
                    continue

        selected.append(r)

    return selected, skipped

# ══════════════════════════════════════════════════════════════════
# FASE 0 — VALIDAÇÃO
# ══════════════════════════════════════════════════════════════════
def fase0_validacao(args):
    plog("=" * 65)
    plog("  FASE 0 — Validação de pré-requisitos")
    plog("=" * 65)

    errors = []
    warnings = []

    # Ferramentas
    for tool in ['aws', 'terraform', 'git']:
        if check_tool(tool):
            plog(f"  ✅ {tool} disponível")
        else:
            errors.append(f"{tool} não encontrado — instale antes de continuar")

    # AWS auth
    ok, out = run("aws sts get-caller-identity --output json")
    if ok:
        identity = json.loads(out) if out else {}
        plog(f"  ✅ AWS autenticado — conta: {identity.get('Account','?')}")
    else:
        errors.append("AWS não autenticado — configure suas credenciais")

    # GitLab token
    token = args.gitlab_token or os.environ.get('GITLAB_TOKEN', '')
    if token:
        plog(f"  ✅ GITLAB_TOKEN disponível")
    else:
        if not args.dry_run:
            errors.append("GITLAB_TOKEN não encontrado — exporte ou use --gitlab-token")
        else:
            warnings.append("GITLAB_TOKEN não definido — fase 4 (MRs) vai falhar")

    # Terraform
    ok, ver = run("terraform version -json 2>/dev/null || terraform version")
    if ok:
        plog(f"  ✅ terraform disponível")
    else:
        errors.append("terraform não encontrado ou com erro")

    if warnings:
        for w in warnings:
            plog(f"  ⚠️  {w}", 'warning')

    if errors:
        plog("")
        plog("  ❌ Pré-requisitos não atendidos:", 'error')
        for e in errors:
            plog(f"     • {e}", 'error')
        sys.exit(1)

    # Ticket obrigatório para approve
    if not args.dry_run and not getattr(args, 'dry_run_full', False):
        ticket = getattr(args, 'ticket', None)
        if not ticket:
            log.error("--ticket é obrigatório para --approve. Ex: --ticket SREK-8432")
            sys.exit(1)
        plog(f"  ✅ Ticket: {ticket}")

    plog(f"  ✅ Pré-requisitos OK")
    plog("")
    return token

# ══════════════════════════════════════════════════════════════════
# FASE 1 — EXTRAÇÃO DE LIFECYCLE
# ══════════════════════════════════════════════════════════════════
def extrair_lifecycle_bucket(bucket_name, lifecycle_dir, dry_run=False):
    out_file = Path(lifecycle_dir) / f"{bucket_name}.lifecycle.json"

    if out_file.exists() and out_file.stat().st_size > 20:
        try:
            data = json.loads(out_file.read_text())
            if 'Rules' in data:
                return 'CACHED', len(data['Rules'])
        except:
            pass

    if dry_run:
        return 'WOULD_EXTRACT', 0

    ok, out = run(
        f"aws s3api get-bucket-lifecycle-configuration --bucket {bucket_name} --output json"
    )
    if ok and out:
        out_file.write_text(out)
        rules = json.loads(out).get('Rules', [])
        return 'EXTRACTED', len(rules)
    elif 'NoSuchLifecycleConfiguration' in out:
        out_file.write_text('{"Rules":[]}')
        return 'NO_LC', 0
    elif 'NoSuchBucket' in out:
        return 'NOT_FOUND', 0
    elif 'AccessDenied' in out:
        return 'ACCESS_DENIED', 0
    else:
        return 'ERROR', 0

def fase1_lifecycle(buckets_lc, lifecycle_dir, args):
    plog("=" * 65)
    plog(f"  FASE 1 — Extração de lifecycle ({len(buckets_lc)} buckets)")
    plog("=" * 65)

    Path(lifecycle_dir).mkdir(parents=True, exist_ok=True)

    results = {'EXTRACTED':0,'CACHED':0,'NO_LC':0,'NOT_FOUND':0,'ACCESS_DENIED':0,'ERROR':0,'WOULD_EXTRACT':0}

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {
            ex.submit(extrair_lifecycle_bucket,
                      r['bucket_name'], lifecycle_dir, args.dry_run): r
            for r in buckets_lc
        }
        for future in as_completed(futures):
            r = futures[future]
            status, count = future.result()
            results[status] = results.get(status, 0) + 1
            icon = {'EXTRACTED':'✅','CACHED':'⏭','NO_LC':'⚪',
                    'NOT_FOUND':'❌','ACCESS_DENIED':'🔒',
                    'ERROR':'❌','WOULD_EXTRACT':'~'}.get(status,'?')
            plog(f"  {icon} [{status}] {r['bucket_name']} ({count} regras)")

    plog("")
    plog(f"  Extraídos:    {results['EXTRACTED']}")
    plog(f"  Do cache:     {results['CACHED']}")
    plog(f"  Sem lifecycle:{results['NO_LC']}")
    plog(f"  Erros:        {results['ERROR'] + results['NOT_FOUND'] + results['ACCESS_DENIED']}")
    plog("")

# ══════════════════════════════════════════════════════════════════
# GERAÇÃO DE ARQUIVOS TERRAFORM
# ══════════════════════════════════════════════════════════════════
def aws_rule_to_bp(rule):
    out = {'id': rule.get('ID','regra'), 'status': rule.get('Status','Enabled')}
    f = rule.get('Filter', {})
    if f:
        fout = {}
        if 'Prefix' in f: fout['prefix'] = f['Prefix']
        if 'Tag' in f: fout['tag'] = [{'key':f['Tag']['Key'],'value':f['Tag']['Value']}]
        if 'And' in f:
            a = f['And']
            if 'Prefix' in a: fout['prefix'] = a['Prefix']
            if 'Tags' in a: fout['tags'] = [{'key':t['Key'],'value':t['Value']} for t in a['Tags']]
        if fout: out['filter'] = fout
    exp = rule.get('Expiration', {})
    if exp:
        eout = {}
        if 'Days' in exp: eout['days'] = exp['Days']
        if 'Date' in exp: eout['date'] = exp['Date']
        if 'ExpiredObjectDeleteMarker' in exp: eout['expired_object_delete_marker'] = exp['ExpiredObjectDeleteMarker']
        if eout: out['expiration'] = eout
    transitions = rule.get('Transitions', [])
    if transitions:
        out['transition'] = [
            {k:v for k,v in {'days':t.get('Days'),'date':t.get('Date'),
             'storage_class':t.get('StorageClass')}.items() if v is not None}
            for t in transitions
        ]
    nve = rule.get('NoncurrentVersionExpiration', {})
    if nve:
        out['noncurrent_version_expiration'] = {k:v for k,v in {
            'noncurrent_days': nve.get('NoncurrentDays'),
            'newer_noncurrent_versions': nve.get('NewerNoncurrentVersions')
        }.items() if v is not None}
    nvt = rule.get('NoncurrentVersionTransitions', [])
    if nvt:
        out['noncurrent_version_transition'] = [
            {k:v for k,v in {'noncurrent_days':t.get('NoncurrentDays'),
             'newer_noncurrent_versions':t.get('NewerNoncurrentVersions'),
             'storage_class':t.get('StorageClass')}.items() if v is not None}
            for t in nvt
        ]
    aimu = rule.get('AbortIncompleteMultipartUpload',{})
    if aimu and 'DaysAfterInitiation' in aimu:
        out['abort_incomplete_multipart_upload'] = {'days_after_initiation': aimu['DaysAfterInitiation']}
    return out

def rules_to_hcl(rules, indent=2):
    pad = ' ' * indent
    pad2 = ' ' * (indent + 2)
    pad3 = ' ' * (indent + 4)
    def val(v):
        if isinstance(v, bool): return 'true' if v else 'false'
        if isinstance(v, str): return f'"{v}"'
        return str(v)
    def render_dict(d, depth=0):
        p = ' ' * (indent + 4 + depth * 2)
        lines = ['{']
        for k, v in d.items():
            if isinstance(v, dict): lines.append(f'{p}{k} = ' + render_dict(v, depth+1))
            elif isinstance(v, list): lines.append(f'{p}{k} = ' + render_list(v, depth+1))
            else: lines.append(f'{p}{k} = {val(v)}')
        lines.append(' ' * (indent + 2 + depth * 2) + '}')
        return '\n'.join(lines)
    def render_list(lst, depth=0):
        if not lst: return '[]'
        p = ' ' * (indent + 4 + depth * 2)
        lines = ['[']
        for item in lst:
            if isinstance(item, dict): lines.append(f'{p}' + render_dict(item, depth+1) + ',')
            else: lines.append(f'{p}{val(item)},')
        lines.append(' ' * (indent + 2 + depth * 2) + ']')
        return '\n'.join(lines)
    hcl = [f'{pad}lifecycle_rules = [']
    for rule in rules:
        hcl.append(f'{pad2}{{')
        for k, v in rule.items():
            if isinstance(v, dict): hcl.append(f'{pad3}{k} = ' + render_dict(v) + ',')
            elif isinstance(v, list): hcl.append(f'{pad3}{k} = ' + render_list(v) + ',')
            else: hcl.append(f'{pad3}{k} = {val(v)},')
        hcl.append(f'{pad2}}},')
    hcl.append(f'{pad}]')
    return '\n'.join(hcl)

def load_lifecycle_hcl(bucket_name, lifecycle_dir):
    lc_file = Path(lifecycle_dir) / f"{bucket_name}.lifecycle.json"
    if not lc_file.exists():
        return None, 0
    try:
        data = json.loads(lc_file.read_text())
        rules = data.get('Rules', [])
        if not rules:
            return None, 0
        bp_rules = [aws_rule_to_bp(r) for r in rules]
        return rules_to_hcl(bp_rules), len(rules)
    except:
        return None, 0

def bp_lifecycle_description(asset_cat, env):
    rules = ["Padrão (sempre): cancela multipart 7 dias + remove delete markers"]
    cat, e = str(asset_cat).strip(), str(env).strip().lower()
    if e in ('dev','hml') and cat != 'Cache':
        rules.append("Expiração dev/hml: objetos expiram após 183 dias")
    if cat in ('Productive data','Model development','Metadata'):
        rules.append("Tiering: 90d → STANDARD_IA | 180d → GLACIER_IR")
    if cat in ('Development','Staging','Sandbox'):
        rules.append("Tiering: 30d → STANDARD_IA | 90d → GLACIER_IR")
    if cat in ('Logs','Backup'):
        rules.append("Tiering: 30d → GLACIER_IR | 120d → GLACIER")
    if cat == 'Cache':
        rules.append("Expiração Cache: objetos expiram após 45 dias")
    return rules

def gen_main_tf(r, lifecycle_hcl, state_bucket, state_region):
    bucket   = r['bucket_name']
    team     = r.get('team','')
    env      = r.get('env','')
    asset    = r.get('asset_category','')
    enc      = r.get('encryption','')
    vers     = r.get('versioning','')
    has_lc   = coerce_bool(r.get('has_lifecycle', False))
    logical, follows = extract_logical(bucket, team, env)
    repo_name = f"ecs-{team}-default-aws-terraform"
    key = f"{repo_name}/services/s3/{logical}/{env}/terraform.tfstate"

    lines = [
        f"# Gerado automaticamente — s3_migrate.py",
        f"# Bucket: {bucket}",
        f"# Data: {datetime.now().strftime('%Y-%m-%d')}",
        f"",
        f'terraform {{',
        f'  backend "s3" {{',
        f'    bucket  = "{state_bucket}"',
        f'    key     = "{key}"',
        f'    region  = "{state_region}"',
        f'  }}',
        f'}}',
        f"",
        f"locals {{",
        f"  tags = {{",
        f'    application      = "PREENCHER"',
        f'    product          = "PREENCHER"',
        f'    environment      = "{env}"',
        f'    team             = "{team}"',
        f'    ticket           = "PREENCHER"',
        f'    appid            = "PREENCHER"',
        f'    businessservices = "PREENCHER"',
        f'    coststring       = "PREENCHER"',
        f'    asset_category   = "{asset}"',
        f'    data_type        = "PREENCHER"',
        f'    data_category    = "PREENCHER"',
        f'    group            = "ecs"',
        f'    repository       = "PREENCHER"',
        f"  }}",
        f"}}",
        f"",
        f'module "s3" {{',
        f'  source = "{BP_SOURCE}"',
        f"",
        f'  tags                  = local.tags',
        f'  tag_legacy_name       = "{bucket}"',
        f'  logging_target_bucket = "{LOGGING_BUCKET}"',
    ]

    if enc == 'AES256':
        lines.append(f'  sse_algorithm         = "AES256"  # OVERRIDE: NÃO alterar para aws:kms')

    if vers in ('Enabled','Suspended'):
        lines.append(f'  versioning_configuration = "{vers}"')

    if lifecycle_hcl:
        lines += ["", "  # Lifecycle extraído da AWS e convertido para formato BP"]
        lines.append(lifecycle_hcl)
    elif has_lc:
        lines += [
            "",
            "  # ⚠️  ATENÇÃO: este bucket tem lifecycle (Categoria B/C2/C4)",
            "  # Execute: aws s3api get-bucket-lifecycle-configuration \\",
            f"  #            --bucket {bucket} > lifecycles/{bucket}.lifecycle.json",
            "  # Depois rode o script novamente para injetar automaticamente",
            "  lifecycle_rules = [",
            "    # PREENCHER — rode o script com o lifecycle extraído",
            "  ]",
        ]

    lines += [f'}}', ""]
    return '\n'.join(lines)

def gen_changes_md(r, lifecycle_hcl, lc_count):
    bucket  = r['bucket_name']
    team    = r.get('team','')
    env     = r.get('env','').upper()
    enc     = r.get('encryption','')
    has_lc  = coerce_bool(r.get('has_lifecycle', False))
    asset   = r.get('asset_category','')
    cat     = r.get('category','')
    vers    = r.get('versioning','')

    if has_lc and lifecycle_hcl:
        risco_geral = '🟡 BAIXO — lifecycle existente preservado'
    elif not has_lc:
        risco_geral = '🟠 MÉDIO — lifecycle será criado pela BP'
    else:
        risco_geral = '🟠 MÉDIO — lifecycle pendente de extração'

    lines = [
        f"# 📦 Import S3 — `{bucket}`",
        f"",
        f"> **Time:** `{team}` | **Env:** `{env}` | **Asset Category:** `{asset}` | **Categoria:** `{cat}`  ",
        f"> **Operação:** Import de bucket existente → Blueprint S3  ",
        f"> **Risco geral:** {risco_geral}",
        f"",
        f"---",
        f"",
        f"## ⚠️ O que muda com o `terraform apply`",
        f"",
        f"> Nenhum recurso será **criado** ou **destruído**.  ",
        f"> A tabela abaixo mostra o que o apply vai **passar a gerenciar** ou **configurar**.",
        f"",
        f"| Recurso AWS | Status | Detalhe | Risco |",
        f"|---|---|---|:---:|",
        f"| `aws_s3_bucket` | ✅ SEM ALTERAÇÃO | Nome `{bucket}` mantido via `tag_legacy_name` | ✅ |",
    ]

    if enc == 'AES256':
        lines.append(
            f"| `aws_s3_bucket_server_side_encryption_configuration` | ✅ SEM ALTERAÇÃO | "
            f"`sse_algorithm = \"AES256\"` explícito — **NÃO trocado para KMS** | ✅ |"
        )
    else:
        lines.append(
            f"| `aws_s3_bucket_server_side_encryption_configuration` | ✅ SEM ALTERAÇÃO | "
            f"`aws:kms` — padrão da BP | ✅ |"
        )

    if lifecycle_hcl:
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | 🟡 PRESERVADO | "
            f"Lifecycle existente ({lc_count} regra(s)) convertido para `lifecycle_rules` | 🟡 |"
        )
    elif has_lc:
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | ⚠️ PENDENTE | "
            f"Lifecycle não extraído ainda — preencher manualmente antes do apply | 🟠 |"
        )
    else:
        lc_rules = bp_lifecycle_description(asset, env.lower())
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | 🟠 NOVO | "
            f"BP vai criar: {' | '.join(lc_rules[:2])} | 🟠 |"
        )

    if vers in ('Enabled','Suspended'):
        lines.append(
            f"| `aws_s3_bucket_versioning` | ✅ SEM ALTERAÇÃO | "
            f"Status `{vers}` mantido via import | ✅ |"
        )

    lines += [
        f"| `aws_s3_bucket_logging` | 🟡 PASSA A GERENCIAR | "
        f"Logging → `{LOGGING_BUCKET}` (bucket já existe) | 🟡 |",
        f"| `aws_s3_bucket_public_access_block` | ✅ SEM ALTERAÇÃO | Block public access mantido | ✅ |",
        f"| `aws_s3_bucket_ownership_controls` | ✅ SEM ALTERAÇÃO | `BucketOwnerEnforced` — padrão BP | ✅ |",
    ]

    if enc == 'AES256':
        lines += [
            f"", f"---", f"",
            f"## 🔒 Encryption — Confirmação obrigatória",
            f"",
            f"```",
            f"╔══════════════════════════════════════════════════════════╗",
            f"║  ⛔  ESTE BUCKET USA AES256                              ║",
            f"║  A Blueprint usa aws:kms por padrão.                    ║",
            f"║  sse_algorithm = \"AES256\" está explícito no main.tf.    ║",
            f"║  ✅  O algoritmo NÃO SERÁ ALTERADO.                     ║",
            f"╚══════════════════════════════════════════════════════════╝",
            f"```",
            f"",
            f"> **No plan:** se aparecer `-/+ replace` em encryption → **NÃO APROVAR**, acionar SRE.",
        ]

    lines += [
        f"", f"---", f"",
        f"## ✅ Checklist do revisor",
        f"",
        f"- [ ] **Nome:** `{bucket}` correto no `tag_legacy_name`",
    ]
    if enc == 'AES256':
        lines.append(f"- [ ] **Encryption:** `sse_algorithm = \"AES256\"` explícito — plan **NÃO** propõe troca para KMS")
    if lifecycle_hcl:
        lines.append(f"- [ ] **Lifecycle:** {lc_count} regra(s) convertida(s) — validar que refletem o estado atual")
    elif has_lc:
        lines.append(f"- [ ] **Lifecycle:** preencher `lifecycle_rules` antes do apply")
    else:
        lines.append(f"- [ ] **Lifecycle:** regras que BP vai criar são adequadas para este bucket")
    if vers in ('Enabled','Suspended'):
        lines.append(f"- [ ] **Versioning:** status `{vers}` mantido")
    lines += [
        f"- [ ] **Terraform plan:** nenhum recurso com `destroy` ou `replace`",
        f"- [ ] **Tags obrigatórias:** campos `PREENCHER` no `main.tf` preenchidos",
        f"",
        f"---",
        f"*Gerado por s3_migrate.py — {datetime.now().strftime('%Y-%m-%d')}*",
    ]
    return '\n'.join(lines)

# ══════════════════════════════════════════════════════════════════
# FASE 2 — GERAÇÃO DE ESTRUTURA
# ══════════════════════════════════════════════════════════════════
def fase2_gerar(bucket, r, lifecycle_dir, output_dir, args):
    # Se os módulos completos estiverem disponíveis, usa extração total
    if HAS_FULL_EXTRACTOR:
        return fase2_gerar_completo(bucket, r, lifecycle_dir, output_dir, args)
    # Fallback: geração básica (só lifecycle)
    return fase2_gerar_legado(bucket, r, lifecycle_dir, output_dir, args)

def fase2_gerar_completo(bucket, r, lifecycle_dir_unused, output_dir, args):
    """Fase 2 com extração completa de todas as configurações do bucket."""
    team     = r.get('team','')
    env      = r.get('env','')
    asset    = r.get('asset_category','')
    category = r.get('category','').strip()
    logical_name, _ = extract_logical(bucket, team, env)
    repo_name   = f"ecs-{team}-default-aws-terraform"
    bucket_path = Path(output_dir) / repo_name / "services" / "s3" / logical_name / env

    # C2: já tem main.tf no repo — gera arquivos atualizados e salva em mr_output
    # (não usa o main.tf do repo diretamente pois pode estar desatualizado)
    if category == 'C2':
        if args.dry_run:
            plog(f"  ~ [DRY-RUN] {bucket} [C2] → geraria main.tf atualizado")
            return str(bucket_path), True
        # Continua para gerar arquivos atualizados em mr_output/
        # A Fase 4 vai copiar de mr_output para o repo e commitar o diff

    if args.dry_run:
        plog(f"  ~ [DRY-RUN] {bucket}")
        plog(f"       → {bucket_path}/")
        plog(f"         versions.tf  backend.tf  main.tf  CHANGES.md  import_commands.sh")
        return str(bucket_path), True

    # Extrai config completo do bucket (todas as 19 configurações)
    configs_dir = Path(output_dir) / '.s3configs'
    configs_dir.mkdir(parents=True, exist_ok=True)
    max_age = getattr(args, 'max_cache_age', None)
    status, cfg = extract_bucket(bucket, str(configs_dir), max_age_hours=max_age)
    if cfg.get('_errors'):
        plog(f"  ⚠️  {bucket} — erros na extração: {list(cfg['_errors'].keys())}", 'warning')

    # ── Diff semântico: compara AWS atual vs estado desejado BP ───────────────
    ticket_val = getattr(args, 'ticket', None) or 'PREENCHER'
    diffs = compute_semantic_diff(
        cfg, bucket, team, env, asset,
        ticket=ticket_val,
        log_bucket='ecs-387979423286-logging-s3',
    )
    diff_report = format_diff_report(diffs, bucket)
    for line in diff_report.split('\n'):
        plog(line)

    mr_needed, mr_reason = needs_mr_from_diff(diffs)
    if not mr_needed:
        plog(f"  ⏭  {bucket} — {mr_reason} — sem MR", 'warning')
        # Salva o resultado para a fase4 usar
        cfg['_diff'] = diffs
        cfg['_needs_mr'] = False
        cfg['_mr_reason'] = mr_reason
    else:
        cfg['_diff'] = diffs
        cfg['_needs_mr'] = True
        cfg['_mr_reason'] = mr_reason

    # Gera todos os arquivos de uma vez
    files = gen_all_files(
        bucket, team, env, asset, cfg,
        args.state_bucket, args.state_region,
        ticket=getattr(args, 'ticket', None) or 'PREENCHER'
    )

    # Cria estrutura de pastas e escreve arquivos
    bucket_path.mkdir(parents=True, exist_ok=True)
    for filename, file_content in files.items():
        # Arquivos prefixados com _ são locais — não vão pro repo
        # Mantém o prefixo _ no disco para que os filtros de cópia os excluam
        if filename.startswith('_'):
            local_path = bucket_path / filename
            local_path.write_text(file_content)
            local_path.chmod(0o755)
            continue
        file_path = bucket_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_content)
        if filename.endswith('.sh'):
            file_path.chmod(0o755)

    file_list = '  '.join(f for f in files.keys() if not f.startswith('_'))
    needs_mr_flag = cfg.get('_needs_mr', True)
    plog(f"  {'✅' if needs_mr_flag else '⏭ '} {bucket}")
    plog(f"       → {bucket_path.relative_to(output_dir)}/")
    plog(f"         {file_list}")
    return str(bucket_path), True, cfg.get('_needs_mr', True), cfg.get('_mr_reason', '')

def fase2_gerar_legado(bucket, r, lifecycle_dir, output_dir, args):
    team    = r.get('team','')
    env     = r.get('env','')
    cat     = r.get('category','')
    has_lc  = coerce_bool(r.get('has_lifecycle', False))
    logical, _ = extract_logical(bucket, team, env)
    repo_name  = f"ecs-{team}-default-aws-terraform"

    bucket_path = Path(output_dir) / repo_name / "services" / "s3" / logical / env

    # Carrega lifecycle se disponível
    lifecycle_hcl, lc_count = None, 0
    if has_lc:
        lifecycle_hcl, lc_count = load_lifecycle_hcl(bucket, lifecycle_dir)

    # Gera conteúdo
    main_tf    = gen_main_tf(r, lifecycle_hcl, args.state_bucket, args.state_region)
    changes_md = gen_changes_md(r, lifecycle_hcl, lc_count)

    if args.dry_run:
        plog(f"  ~ [DRY-RUN] {bucket} → {bucket_path}")
        return str(bucket_path), True

    bucket_path.mkdir(parents=True, exist_ok=True)
    (bucket_path / "main.tf").write_text(main_tf)
    (bucket_path / "CHANGES.md").write_text(changes_md)

    plog(f"  ✅ {bucket} → {bucket_path.relative_to(output_dir)}")
    return str(bucket_path), True

def fase2_gerar_todos(buckets, lifecycle_dir, output_dir, args):
    plog("=" * 65)
    plog(f"  FASE 2 — Geração de estrutura Terraform ({len(buckets)} buckets)")
    plog("=" * 65)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    bucket_paths   = {}
    bucket_mr_flags = {}  # bucket → (needs_mr, reason)
    ok_count = 0

    for r in sorted(buckets, key=lambda x: (x.get('team',''), x.get('env',''), x.get('bucket_name',''))):
        bucket = r['bucket_name']
        result = fase2_gerar(bucket, r, lifecycle_dir, output_dir, args)
        if isinstance(result, tuple) and len(result) >= 2:
            path, ok = result[0], result[1]
            needs_mr = result[2] if len(result) > 2 else True
            mr_reason = result[3] if len(result) > 3 else ''
        else:
            path, ok, needs_mr, mr_reason = result, False, True, ''
        if ok:
            bucket_paths[bucket] = path
            bucket_mr_flags[bucket] = (needs_mr, mr_reason)
            ok_count += 1

    plog(f"\n  Gerados: {ok_count} buckets")
    sem_mr = sum(1 for v in bucket_mr_flags.values() if not v[0])
    if sem_mr:
        plog(f"  ⏭  {sem_mr} sem MR (só mudanças cosméticas)")
    plog("")
    return bucket_paths, bucket_mr_flags

# ══════════════════════════════════════════════════════════════════
# FASE 2b — MIGRAÇÃO DE STATE (C3/C4)
# ══════════════════════════════════════════════════════════════════
def check_tfstate_version(state_bucket, state_key):
    """
    Verifica se o tfstate é compatível com Terraform >= 0.13.
    Retorna (compativel: bool, versao: str)
    Tfstates criados antes de 0.13 usam provider addresses sem namespace
    (ex: "aws" em vez de "registry.terraform.io/hashicorp/aws")
    """
    ok, out = run(
        f"aws s3 cp s3://{state_bucket}/{state_key} - 2>/dev/null"
    )
    if not ok or not out:
        return True, 'desconhecida'  # Não existe — sem problema

    try:
        state = json.loads(out)
        version = state.get('terraform_version', '0.0.0')
        serial  = state.get('serial', 0)

        # Verifica se tem providers no formato legado
        # Em 0.12: providers são strings simples como "provider.aws"
        # Em 0.13+: "registry.terraform.io/hashicorp/aws"
        providers = state.get('provider_schemas', {})
        resources = state.get('resources', [])

        legacy_providers = set()
        for res in resources:
            prov = res.get('provider', '')
            # Formato legado: "provider["registry.terraform.io/hashicorp/aws"]"
            # Formato 0.12:   "provider.aws" ou sem namespace
            if prov and 'registry.terraform.io' not in prov and 'provider.' in prov:
                legacy_providers.add(prov)

        if legacy_providers:
            return False, version

        # Verifica versão diretamente
        parts = version.split('.')
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0

        if major == 0 and minor < 13:
            return False, version

        return True, version
    except Exception:
        return True, 'desconhecida'  # Na dúvida, tenta usar


def fase2b_state_mv(buckets_legado, bucket_paths, args):
    c34 = [r for r in buckets_legado
           if r.get('category','') in ('C3','C4')
           and r.get('state_key_atual') and r['state_key_atual'] != 'nan']

    legacy_states = set()
    if not c34:
        return legacy_states

    use_state_mv = getattr(args, 'state_mv', False)

    plog("=" * 65)
    plog(f"  FASE 2b — State C3/C4 ({len(c34)} buckets)")
    plog("=" * 65)
    if not use_state_mv:
        plog("  Modo: import limpo (padrão) — NÃO copia tfstate legado para module.s3")
        plog("  Use --state-mv só se houver refactor de endereços validado (raro).")

    for r in c34:
        bucket     = r['bucket_name']
        team       = r.get('team','')
        env        = r.get('env','')
        old_key    = r.get('state_key_atual','').strip()
        logical, _ = extract_logical(bucket, team, env)
        repo_name  = f"ecs-{team}-default-aws-terraform"
        new_key    = f"{repo_name}/services/s3/{logical}/{env}/terraform.tfstate"

        if not old_key or old_key == 'nan':
            plog(f"  ⚠️  {bucket} — state_key_atual vazio, pulando", 'warning')
            continue

        plog(f"\n  📦 {bucket}")
        plog(f"     DE:  {old_key}")
        plog(f"     PARA: {new_key}")

        if args.dry_run:
            if use_state_mv:
                plog(f"     ~ [DRY-RUN] verificaria versão e copiaria state")
            else:
                plog(f"     ~ [DRY-RUN] apagaria state destino + import limpo Fase 3")
            legacy_states.add(bucket)
            continue

        # Padrão Blueprint S3: nunca copiar state legado (endereços module.s3.module.s3.*)
        if not use_state_mv:
            plog(f"     ⏭  Import limpo — state legado não copiado")
            run(f"aws s3 rm s3://{args.state_bucket}/{new_key} 2>/dev/null")
            legacy_states.add(bucket)
            continue

        # ── Verifica compatibilidade do tfstate antes de copiar (--state-mv) ──
        compativel, tf_version = check_tfstate_version(args.state_bucket, old_key)
        if not compativel:
            plog(f"     ⚠️  State legado em formato Terraform {tf_version} (< 0.13)", 'warning')
            plog(f"     ⏭  Pulando cópia de state — será feito import limpo na Fase 3")
            legacy_states.add(bucket)
            # Garante que o state no novo path não existe (evita conflito)
            run(f"aws s3 rm s3://{args.state_bucket}/{new_key} 2>/dev/null")
            continue

        plog(f"     ✅ State compatível (Terraform {tf_version})")

        # Copia o state para o novo path
        cmd_cp = (
            f"aws s3 cp "
            f"s3://{args.state_bucket}/{old_key} "
            f"s3://{args.state_bucket}/{new_key}"
        )
        ok, out = run(cmd_cp)
        if ok:
            plog(f"     ✅ State copiado para novo path")
        else:
            plog(f"     ❌ Erro ao copiar state: {out}", 'error')
            continue

        # Se tinha state duplicado, remove o legado
        if coerce_bool(r.get('legado_cleanup_needed', False)):
            cmd_rm = f"aws s3 rm s3://{args.state_bucket}/{old_key}"
            ok2, out2 = run(cmd_rm)
            if ok2:
                plog(f"     🗑️  State legado removido")
            else:
                plog(f"     ⚠️  Não foi possível remover state legado: {out2}", 'warning')

    if legacy_states:
        plog(f"\n  ℹ️  {len(legacy_states)} bucket(s) com state legado — import limpo na Fase 3:")
        for b in sorted(legacy_states):
            plog(f"     • {b}")

    plog("")
    return legacy_states

# ══════════════════════════════════════════════════════════════════
# FASE 3 — DRY-RUN TERRAFORM
# ══════════════════════════════════════════════════════════════════
def fase3_terraform_plan(bucket_paths, repos_dir, args, legacy_states=None):
    plog("=" * 65)
    plog(f"  FASE 3 — Terraform plan ({len(bucket_paths)} buckets)")
    plog("=" * 65)

    plan_results = []
    blocked = []
    legacy_states = legacy_states or set()

    for bucket, path in bucket_paths.items():
        bucket_dir = Path(path)

        plog(f"\n  📋 {bucket}")

        if args.dry_run:
            plog(f"     ~ [DRY-RUN] terraform init + plan em {bucket_dir}")
            plan_results.append({'bucket': bucket, 'status': 'DRY_RUN', 'issues': []})
            continue

        if not bucket_dir.exists():
            plog(f"  ⚠️  {bucket} — pasta não encontrada: {path}", 'warning')
            continue

        # terraform fmt
        run("terraform fmt -no-color 2>&1", cwd=str(bucket_dir))

        # terraform init com retry (GitLab pode throttlear download do módulo)
        ok_init, out_init = False, ''
        for attempt in range(1, 4):
            ok_init, out_init = run(
                "terraform init -input=false -no-color 2>&1",
                cwd=str(bucket_dir)
            )
            if ok_init:
                break
            if 'Invalid legacy provider' in out_init:
                break  # state legado — sem retry
            if attempt < 3:
                wait = attempt * 10
                plog(f"     ⏳ Init falhou (tentativa {attempt}/3) — aguardando {wait}s...", 'warning')
                import time as _time; _time.sleep(wait)

        if not ok_init:
            plog(f"     ❌ terraform init falhou: {out_init[:300]}", 'error')
            plan_results.append({'bucket': bucket, 'status': 'INIT_FAILED',
                                 'issues': [out_init[:300]], 'needs_mr': False, 'skip_reason': ''})
            blocked.append(bucket)
            continue

        # terraform import — pula para C2 (já tem state no novo repo)
        # Para buckets com state legado 0.12, o import_commands.sh vai
        # criar um state novo limpo (o state legado foi descartado)
        import_sh = bucket_dir / "_import_commands.sh"
        bucket_in_results = next((r for r in plan_results if r['bucket'] == bucket), {})
        already_imported = bucket_in_results.get('_imported', False)

        if bucket in legacy_states:
            plog(f"     ℹ️  State legado 0.12 — fazendo import limpo")

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

        # terraform plan — salva binário para gerar JSON depois (plan_reviewer)
        ok_plan, out_plan = run(
            "terraform plan -input=false -no-color -out=tfplan.binary 2>&1",
            cwd=str(bucket_dir)
        )
        # Gera plan_output.json para o plan_reviewer.py
        if ok_plan:
            _, out_json = run("terraform show -json tfplan.binary 2>&1", cwd=str(bucket_dir))
            if out_json.strip().startswith('{'):
                (bucket_dir / "plan_output.json").write_text(out_json)
        run("rm -f tfplan.binary 2>&1", cwd=str(bucket_dir))

        issues = []
        if import_failed:
            issues.append('IMPORT_FALHOU — verificar _import_error.txt e importar manualmente')
        # Detecta operações perigosas
        warnings_plan = []

        # 1. Replace/destroy de recurso principal — SEMPRE BLOQUEANTE
        if 'must be replaced' in out_plan or '# forces replacement' in out_plan:
            issues.append('REPLACE_DETECTADO')

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

        # 2. Mudança só em tags — NÃO bloqueia, só avisa
        tag_only_change = False
        if not issues and out_plan:
            lines_with_changes = [l for l in plan_lines
                                  if l.strip().startswith(('+', '-', '~'))
                                  and 'tags' not in l.lower()
                                  and '#' not in l.strip()[:2]]
            tag_lines = [l for l in plan_lines
                        if l.strip().startswith(('+', '-', '~'))
                        and 'tags' in l.lower()]
            if tag_lines and not lines_with_changes:
                tag_only_change = True
                warnings_plan.append('MUDANCA_SOMENTE_TAGS — não bloqueante')

        # 3. Mudança em lifecycle — NÃO bloqueia, avisa
        if 'lifecycle_rule' in out_plan.lower() or 'lifecycle_configuration' in out_plan.lower():
            if any(l.strip().startswith(('+', '-', '~')) and 'lifecycle' in l.lower()
                   for l in plan_lines):
                warnings_plan.append('MUDANCA_LIFECYCLE — verificar se regras estão corretas')

        # 4. Erro no plan
        if 'Error' in out_plan and not ok_plan:
            issues.append('PLAN_ERROR')
            # Loga as linhas de erro para facilitar debug
            for line in out_plan.split('\n'):
                if line.strip().startswith('Error') or 'Error:' in line:
                    plog(f"     ❌ {line.strip()}", 'error')

        # Loga warnings sem bloquear
        for w in warnings_plan:
            plog(f"     ⚠️  {w}", 'warning')

        if issues:
            status = 'BLOCKED'
            plog(f"     🔴 BLOQUEADO — {', '.join(issues)}", 'error')
            for line in out_plan.split('\n'):
                if any(x in line for x in ['must be replaced', 'forces replacement']):
                    plog(f"        {line.strip()}", 'error')
            # Salva output completo para debug
            error_file = bucket_dir / "_plan_error.txt"  # prefixo _ = não vai pro repo
            error_file.write_text(out_plan)
            plog(f"     📄 Output completo salvo em: {error_file}", 'warning')
            blocked.append(bucket)
            plan_results.append({'bucket': bucket, 'status': 'BLOCKED',
                                 'issues': issues, 'needs_mr': False,
                                 'skip_reason': '', 'plan_output': out_plan[:3000]})
        else:
            NOISE_TAGS   = {'ticket','appliedat','managedat','updatedat','createdby'}
            plan_lines   = out_plan.split('\n')
            changes_line = next((l for l in plan_lines
                                if 'to add' in l or 'to change' in l
                                or 'to destroy' in l), '')
            no_changes   = ('No changes' in out_plan or
                           'no changes' in out_plan.lower() or
                           not changes_line.strip())

            has_lifecycle_change     = False
            has_real_tag_change      = False
            changed_resources        = []

            for line in plan_lines:
                s = line.strip()
                if s.startswith('# ') and ' will be ' in s:
                    if 'lifecycle_configuration' in s.lower():
                        has_lifecycle_change = True
                    elif 'tags_all' not in s:
                        changed_resources.append(s)
                elif s.startswith(('+ "', '- "', '~ "')):
                    try:
                        tag_name = s.split('"')[1].lower()
                        if tag_name not in NOISE_TAGS:
                            has_real_tag_change = True
                    except Exception:
                        pass

            has_real_resource_change = bool(
                changed_resources and
                not all('lifecycle' in r.lower() or 'tags_all' in r.lower()
                        for r in changed_resources)
            )

            if no_changes:
                needs_mr    = False
                skip_reason = 'No changes — bucket já está correto'
                status      = 'NO_CHANGES'
            elif not has_real_tag_change and not has_lifecycle_change and not has_real_resource_change:
                needs_mr    = False
                skip_reason = 'Só Ticket/AppliedAt — sem impacto real'
                status      = 'TAG_NOISE_ONLY'
            else:
                needs_mr    = True
                skip_reason = ''
                status      = 'OK'

            plog(f"     {'✅' if needs_mr else '⏭ '} Plan {'OK — abrirá MR' if needs_mr else 'sem MR'} — {changes_line.strip()}")
            if not needs_mr:
                plog(f"        → {skip_reason}")
            else:
                for res in changed_resources[:4]:
                    plog(f"        {res}")
                if has_lifecycle_change:
                    plog(f"        ⚠️  Lifecycle será ajustado")
                if has_real_tag_change:
                    plog(f"        🏷️  Tags com mudanças relevantes")

            plan_results.append({'bucket': bucket, 'status': status,
                                 'issues': [], 'needs_mr': needs_mr,
                                 'skip_reason': skip_reason,
                                 'plan_output': out_plan[:3000]})

    # Gera plan_report.md
    report_lines = [
        f"# Plan Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        f"Total: {len(plan_results)} | "
        f"OK: {sum(1 for r in plan_results if r['status']=='OK')} | "
        f"Bloqueados: {len(blocked)} | "
        f"Dry-run: {sum(1 for r in plan_results if r['status']=='DRY_RUN')}",
        f"",
    ]
    if blocked:
        report_lines += [f"## ❌ Bloqueados ({len(blocked)})", ""]
        for b in blocked:
            r = next((x for x in plan_results if x['bucket']==b), {})
            report_lines.append(f"- `{b}` — {', '.join(r.get('issues',[]))}")
        report_lines.append("")

    no_mr = sum(1 for r in plan_results if not r.get('needs_mr',True) and r['status'] not in ('BLOCKED','INIT_FAILED'))
    report_lines += [f"Sem MR: {no_mr} (No changes ou só ruído de tag)", f"", f"## Todos os buckets", ""]
    for r in plan_results:
        icon = {'OK':'✅','BLOCKED':'🔴','DRY_RUN':'~','INIT_FAILED':'❌','NO_CHANGES':'⏭','TAG_NOISE_ONLY':'⏭'}.get(r['status'],'?')
        note = f" — {r.get('skip_reason','')}" if not r.get('needs_mr',True) else ''
        report_lines.append(f"- {icon} `{r['bucket']}` [{r['status']}]{note}")

    report_path = Path(args.output_dir) / "plan_report.md"
    report_path.write_text('\n'.join(report_lines))

    plog(f"\n  Plan report salvo em: {report_path}")
    plog(f"  OK: {sum(1 for r in plan_results if r['status']=='OK')}")
    plog(f"  Bloqueados: {len(blocked)}")
    plog("")

    if blocked and not args.dry_run:
        plog("  ⚠️  Há buckets bloqueados. Revise o plan_report.md antes de continuar.", 'warning')

    return plan_results, blocked

# ══════════════════════════════════════════════════════════════════
# FASE 4 — CLONE, COMMIT E MRs
# ══════════════════════════════════════════════════════════════════
def get_or_clone_repo(repo_name, repos_dir, gitlab_url, token, dry_run=False):
    """
    Clona o repo respeitando o subgroup do time no GitLab.
    Padrão ECS: gitlab.ecsbr.net/ecs/{team}/{repo_name}
    O team é extraído do repo_name: ecs-{team}-default-aws-terraform → {team}
    """
    repo_path = (Path(repos_dir).expanduser().resolve() / repo_name)
    if repo_path.exists():
        git_dir = repo_path / '.git'
        if not git_dir.exists():
            plog(
                f"  ❌ {repo_path} existe mas não é clone git (sem .git). "
                f"Remova a pasta e rode de novo para clonar, ex.: rm -rf {repo_path}",
                'error',
            )
            return None
        plog(f"  📁 {repo_name} — já existe localmente")
        ok, out = run("git pull --rebase origin main 2>&1", cwd=str(repo_path), dry_run=dry_run)
        if not ok and not dry_run:
            plog(f"  ⚠️  git pull falhou (continuando com o clone local): {out[:400]}", 'warning')
        return str(repo_path.resolve())

    # Extrai o subgroup (team) do nome do repo
    # ecs-collection-default-aws-terraform → collection
    # ecs-lno-default-aws-terraform → lno
    import re as _re
    m = _re.match(r'ecs-([a-z0-9]+)-default-aws-terraform', repo_name)
    subgroup = m.group(1) if m else 'ecs'

    # URL com subgroup: ecs/{team}/{repo_name}
    base = gitlab_url.replace('https://', '')
    repo_url = f"https://oauth2:{token}@{base}/ecs/{subgroup}/{repo_name}.git"

    plog(f"  📥 Clonando {repo_name}...")
    plog(f"     → {gitlab_url}/ecs/{subgroup}/{repo_name}")

    if dry_run:
        plog(f"  ~ [DRY-RUN] git clone {gitlab_url}/ecs/{subgroup}/{repo_name}")
        repo_path.mkdir(parents=True, exist_ok=True)
        return str(repo_path.resolve())

    ok, out = run(f"git clone {repo_url} {repo_path} 2>&1")
    if not ok:
        plog(f"  ❌ Falha ao clonar {repo_name}: {out[:300]}", 'error')
        return None
    return str(repo_path.resolve())

def build_mr_title_desc_labels(bucket, src, rel):
    """Monta título, descrição (CHANGES.md completo) e labels para a MR."""
    enc_flag = lc_flag = log_flag = sns_flag = rep_flag = ''
    main_tf_p = src / 'main.tf'
    changes_p = src / 'CHANGES.md'
    if main_tf_p.exists():
        _mtxt = main_tf_p.read_text()
        if 'AES256' in _mtxt:
            enc_flag = ' 🔒 AES256'
        if 'lifecycle_rules = [' in _mtxt or 'lifecycle_rules=[' in _mtxt:
            lc_flag = ' ⚠️ lifecycle preservado'
        elif 'Sem lifecycle configurado — BP vai criar' in _mtxt:
            lc_flag = ' ℹ️ lifecycle-bp-novo'
        elif 'coincidem com o padrão BP' in _mtxt:
            lc_flag = ' ✅ lifecycle-bp'
    if changes_p.exists():
        _ctxt = changes_p.read_text()
        if '🟠 ALTERAÇÃO' in _ctxt and 'logging' in _ctxt.lower():
            log_flag = ' 🔄 logging'
        if 'SNS' in _ctxt and 'TopicConfiguration' in _ctxt:
            sns_flag = ' 📢 SNS'
        if 'replication' in _ctxt.lower() and 'VERIFICAR' in _ctxt:
            rep_flag = ' 🔁 replication'
    title = f"feat(s3): import {bucket}{enc_flag}{lc_flag}{log_flag}{sns_flag}{rep_flag}"
    _labels = ['s3-migration']
    if enc_flag:
        _labels.append('s3-aes256')
    if lc_flag:
        _labels.append('s3-lifecycle')
        if 'bp-novo' in lc_flag:
            _labels.append('s3-lifecycle-bp-create')
        if 'preservado' in lc_flag:
            _labels.append('s3-lifecycle-custom')
    if log_flag:
        _labels.append('s3-logging-change')
    if sns_flag:
        _labels.append('s3-sns')
    if rep_flag:
        _labels.append('s3-replication')
    _MR_DESC_MAX = 100000
    if changes_p.exists():
        _body = changes_p.read_text().strip()
        if len(_body) > _MR_DESC_MAX:
            desc = (
                _body[:_MR_DESC_MAX]
                + f"\n\n---\n\n_…descrição truncada em {_MR_DESC_MAX} caracteres; "
                f"trecho final em `{rel}/CHANGES.md` no branch._"
            )
        else:
            desc = _body
        desc += (
            "\n\n---\n\n"
            "> ✅ **Pipeline:** confira o `terraform plan` no job da MR antes de aprovar merge/apply."
        )
    else:
        desc = f"Import do bucket `{bucket}` via Blueprint S3."
    return title, desc, _labels


def atualizar_mr_gitlab_description(repo_name, branch, gitlab_url, token, description, dry_run=False):
    """PUT na MR aberta da branch — preenche Overview com CHANGES.md (ex.: MRs antigas)."""
    if dry_run:
        plog(f"  ~ [DRY-RUN] Atualizar descrição MR branch={branch}")
        return True
    import urllib.request, urllib.parse
    import re as _re4
    _m = _re4.match(r'ecs-([a-z0-9]+)-default-aws-terraform', repo_name)
    _sg = _m.group(1) if _m else 'ecs'
    project_path = urllib.parse.quote(f"ecs/{_sg}/{repo_name}", safe='')
    list_url = (
        f"{gitlab_url}/api/v4/projects/{project_path}/merge_requests"
        f"?state=opened&source_branch={urllib.parse.quote(branch)}"
    )
    try:
        req = urllib.request.Request(list_url, headers={"PRIVATE-TOKEN": token})
        with urllib.request.urlopen(req, timeout=20) as resp:
            mrs = json.loads(resp.read())
        if not mrs:
            return False
        iid = mrs[0]['iid']
        put_url = f"{gitlab_url}/api/v4/projects/{project_path}/merge_requests/{iid}"
        payload = json.dumps({"description": description}).encode()
        req2 = urllib.request.Request(
            put_url,
            data=payload,
            headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req2, timeout=45) as resp2:
            if resp2.status not in (200, 201):
                return False
        return True
    except Exception as e:
        plog(f"    ⚠️  atualizar_mr_gitlab_description: {e}", 'warning')
        return False


def abrir_mr_gitlab(repo_name, branch, gitlab_url, token, title, description, dry_run=False, labels=None):
    if dry_run:
        plog(f"  ~ [DRY-RUN] Abrir MR: {title}")
        return 'DRY_RUN_URL'

    import urllib.request, urllib.parse
    # Subgroup correto: ecs/{team}/{repo_name}
    import re as _re3
    _m = _re3.match(r'ecs-([a-z0-9]+)-default-aws-terraform', repo_name)
    _sg = _m.group(1) if _m else 'ecs'
    project_path = urllib.parse.quote(f"ecs/{_sg}/{repo_name}", safe='')
    api_url = f"{gitlab_url}/api/v4/projects/{project_path}/merge_requests"

    payload = json.dumps({
        "source_branch": branch,
        "target_branch": "main",
        "title": title,
        "description": description,
        "remove_source_branch": True,
        "labels": ','.join(labels) if labels else 's3-migration',
    }).encode()

    req = urllib.request.Request(
        api_url, data=payload,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get('web_url', '?')
    except Exception as e:
        return f"ERRO: {e}"

def _pre_mr_confirmation(ok_buckets, bucket_paths, plan_results, args):
    """
    Mostra resumo consolidado de todos os plans ANTES de abrir qualquer MR.
    Retorna o conjunto de buckets aprovados pelo usuário.
    - BLOCKED  → excluído automaticamente, sem pergunta
    - REVIEW   → pergunta individual com contexto do plan
    - SAFE/INFO → uma única pergunta em lote para todos
    - auto_confirm → pula tudo, aprova todos
    """
    # Aplica --exclude antes de qualquer confirmação
    excluded_cli = set(getattr(args, 'exclude', None) or [])
    if excluded_cli:
        for b in sorted(excluded_cli & set(ok_buckets)):
            plog(f"  ⏭  {b} — excluído via --exclude")
        ok_buckets = [b for b in ok_buckets if b not in excluded_cli]

    if getattr(args, 'auto_confirm', False):
        plog(f"  Auto-confirm ativo — abrindo MRs para todos os {len(ok_buckets)} buckets")
        return set(ok_buckets)

    if not ok_buckets:
        return set()

    # Tenta carregar o plan_reviewer para classificar cada bucket
    try:
        from plan_reviewer import review_plan as _review_plan
        has_reviewer = True
    except ImportError:
        has_reviewer = False

    verdicts = {}
    for bucket in ok_buckets:
        path = bucket_paths.get(bucket, '')
        plan_json_path = Path(path) / 'plan_output.json' if path else None
        if has_reviewer and plan_json_path and plan_json_path.exists():
            try:
                import json as _json
                plan = _json.loads(plan_json_path.read_text())
                result = _review_plan(plan, bucket)
                verdicts[bucket] = result['verdict']
            except Exception:
                verdicts[bucket] = 'UNKNOWN'
        else:
            verdicts[bucket] = 'UNKNOWN'

    clean    = sorted(b for b in ok_buckets if verdicts.get(b) in ('SAFE', 'INFO', 'UNKNOWN'))
    needs_review = sorted(b for b in ok_buckets if verdicts.get(b) == 'REVIEW')
    blocked  = sorted(b for b in ok_buckets if verdicts.get(b) == 'BLOCKED')

    plog("")
    plog("=" * 65)
    plog("  PRÉ-CONFIRMAÇÃO — Resumo antes de abrir MRs")
    plog("=" * 65)
    plog(f"  Buckets prontos no plan: {len(ok_buckets)}")
    plog(f"  ✅ Limpos (confirmar em lote): {len(clean)}")
    plog(f"  🟡 Requerem revisão (confirmar um a um): {len(needs_review)}")
    plog(f"  🔴 Bloqueados (excluídos automaticamente): {len(blocked)}")
    plog("")

    approved = set()

    # Bloqueados — excluídos sem pergunta
    if blocked:
        plog("  🔴 EXCLUÍDOS automaticamente (BLOCKED no review):")
        for b in blocked:
            plog(f"     ❌ {b}")
        plog("")

    # REVIEW — pergunta individual com contexto do plan
    if needs_review:
        plog("  🟡 Buckets que precisam de revisão humana:")
        for b in needs_review:
            plan_entry = next((r for r in plan_results if r['bucket'] == b), {})
            plan_out   = plan_entry.get('plan_output', '')
            plog(f"\n  {'─'*55}")
            plog(f"  Bucket: {b}")
            # Mostra linhas relevantes do plan (lifecycle, policy, encryption)
            shown = 0
            for line in plan_out.split('\n'):
                s = line.strip()
                is_resource = s.startswith('# ') and ' will be ' in s
                is_relevant = any(kw in s.lower() for kw in
                                  ['lifecycle', 'policy', 'encryption', 'versioning',
                                   'replication', 'logging'])
                if (is_resource or is_relevant) and s.startswith(('#', '~', '+', '-', 'Plan:')):
                    plog(f"     {s[:90]}")
                    shown += 1
                    if shown >= 15:
                        plog(f"     ... (ver plan_output.json para mais detalhes)")
                        break
            plog(f"  {'─'*55}")
            try:
                resp = input(f"  Incluir {b} na MR? [s/N/abort] ").strip().lower()
            except EOFError:
                resp = 's'
            if resp in ('abort', 'a'):
                plog("  🛑 Abortado pelo usuário")
                return approved
            if resp in ('s', 'sim', 'y', 'yes'):
                approved.add(b)
                plog(f"  ✅ {b} incluído")
            else:
                plog(f"  ⏭  {b} excluído pelo usuário")
        plog("")

    # CLEAN — uma única confirmação em lote (com opção de excluir por número)
    if clean:
        plog(f"  ✅ Buckets sem problemas detectados ({len(clean)}):")
        for i, b in enumerate(clean, 1):
            plog(f"     [{i:2d}] ✅ {b}")
        plog("")
        plog("  Opções: s = abrir todas | n = cancelar todas")
        plog("          excluir 2,5,7 = abrir todas exceto os números listados")
        try:
            resp = input(f"  Abrir MR para os {len(clean)} bucket(s) limpos? [s/N/excluir N,...] ").strip().lower()
        except EOFError:
            resp = 's'
        if resp in ('s', 'sim', 'y', 'yes', ''):
            approved.update(clean)
            plog(f"  ✅ {len(clean)} buckets aprovados para MR")
        elif resp.startswith('excluir') or resp.startswith('ex ') or resp.startswith('e '):
            # Parseia os números a excluir
            nums_str = resp.split(None, 1)[1] if ' ' in resp else ''
            try:
                exclude_nums = {int(x.strip()) for x in nums_str.split(',') if x.strip()}
            except ValueError:
                exclude_nums = set()
            for i, b in enumerate(clean, 1):
                if i not in exclude_nums:
                    approved.add(b)
                else:
                    plog(f"  ⏭  {b} — excluído pelo usuário")
            plog(f"  ✅ {len(approved)} buckets aprovados para MR")
        else:
            plog(f"  ⏭  Nenhuma MR aberta para os buckets limpos")

    plog("")
    plog(f"  Total aprovados: {len(approved)} / {len(ok_buckets)}")
    plog("=" * 65)
    return approved


def fase4_mrs(bucket_paths, plan_results, args, token, bucket_mr_flags=None):
    plog("=" * 65)
    plog(f"  FASE 4 — Commit, push e abertura de MRs")
    plog(f"  Modo: {'1 MR por bucket' if args.one_mr_per_bucket else '1 MR por repo de time'}")
    plog("=" * 65)

    # bucket_mr_flags vem da fase2 (diff semântico)
    if args.dry_run:
        ok_buckets = set(bucket_paths.keys())
    else:
        # Filtra pelo diff semântico E pelo plan result
        ok_buckets = set()
        for r in plan_results:
            if r['status'] not in ('OK','DRY_RUN'):
                continue
            if not r.get('needs_mr', True):
                continue
            # Verifica também o flag do diff semântico (passado como parâmetro)
            sem_diff = bucket_mr_flags or {}
            if r['bucket'] in sem_diff and not sem_diff[r['bucket']][0]:
                plog(f"  ⏭  {r['bucket']} — {sem_diff[r['bucket']][1]} — sem MR")
                continue
            ok_buckets.add(r['bucket'])
        skipped_mrs = [r for r in plan_results
                      if not r.get('needs_mr', True) and r['status'] not in ('BLOCKED',)]
        if skipped_mrs:
            plog(f"  Buckets sem MR necessária: {len(skipped_mrs)}")
            for r in skipped_mrs:
                plog(f"    ⏭  {r['bucket']} — {r.get('skip_reason','')}")

        # Pré-confirmação consolidada antes de abrir qualquer MR
        ok_buckets = _pre_mr_confirmation(ok_buckets, bucket_paths, plan_results, args)

    mr_urls = []
    date_tag = datetime.now().strftime('%Y%m%d')

    # Agrupa por repo de time
    repos = {}
    for bucket, path in bucket_paths.items():
        if bucket not in ok_buckets:
            continue
        parts = Path(path).parts
        repo_name = next((p for p in parts if re.match(r'ecs-\w+-\w+-aws-terraform', p)), None)
        if not repo_name:
            continue
        repos.setdefault(repo_name, []).append((bucket, path))

    for repo_name, buckets in sorted(repos.items()):
        plog(f"\n  📁 {repo_name} ({len(buckets)} buckets)")

        # Clone ou pull do repo uma vez por time
        repo_path = get_or_clone_repo(
            repo_name, args.repos_dir,
            args.gitlab_url, token, args.dry_run
        )
        if not repo_path:
            continue
        repo_path = str(Path(repo_path).resolve())

        if args.one_mr_per_bucket:
            # ── UMA MR POR BUCKET ──────────────────────────────────────────────
            for bucket, path in buckets:
                # Branch único por bucket
                ticket_slug = getattr(args, 'ticket', 'PREENCHER').upper()
                branch_name = f"feature/{ticket_slug}-import-s3-{bucket}"
                # Trunca se muito longo (GitLab limita em 255 chars)
                if len(branch_name) > 200:
                    branch_name = branch_name[:200]

                plog(f"\n    📦 {bucket}")

                if args.dry_run:
                    plog(f"    ~ [DRY-RUN] branch={branch_name} → MR individual")
                    mr_urls.append({'repo': repo_name, 'bucket': bucket,
                                   'url': 'DRY_RUN', 'branch': branch_name})
                    continue

                # Path relativo services/s3/... (precisa existir antes de qualquer uso de rel)
                src = Path(path)
                rel = None
                for i, part in enumerate(src.parts):
                    if part == 'services':
                        rel = Path(*src.parts[i:])
                        break
                if not rel:
                    plog(f"    ❌ Não foi possível determinar path relativo de {src}", 'error')
                    continue

                # Reset limpo para main antes de cada bucket
                ok_main, out_main = run("git checkout main 2>&1", cwd=repo_path)
                if not ok_main:
                    plog(f"    ❌ git checkout main falhou em {repo_path}: {out_main[:400]}", 'error')
                    continue
                run("git fetch origin 2>&1", cwd=repo_path)
                run("git reset --hard origin/main 2>&1", cwd=repo_path)
                run("git clean -fd 2>&1", cwd=repo_path)

                import re as _re2
                import urllib.request, urllib.parse
                m2 = _re2.match(r'ecs-([a-z0-9]+)-default-aws-terraform', repo_name)
                sg = m2.group(1) if m2 else 'ecs'
                project_path = urllib.parse.quote(f"ecs/{sg}/{repo_name}", safe='')

                # MR já aberta para esta branch — ainda assim fazemos push para preencher diff (ex.: MR vazia)
                existing_open_mr_url = None
                check_url = (
                    f"{args.gitlab_url}/api/v4/projects/{project_path}"
                    f"/merge_requests?source_branch={urllib.parse.quote(branch_name)}"
                    f"&state=opened"
                )
                try:
                    req = urllib.request.Request(
                        check_url, headers={"PRIVATE-TOKEN": token}
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        _mrs_open = json.loads(resp.read())
                        if _mrs_open:
                            existing_open_mr_url = _mrs_open[0].get('web_url', '?')
                            plog(
                                f"    ⏭  MR já aberta — atualizando branch com mr_output: "
                                f"{existing_open_mr_url}"
                            )
                except Exception as e:
                    plog(f"    ⚠️  Erro ao listar MRs abertas: {e}", 'warning')

                branch_ref = f"refs/heads/{branch_name}"
                ok_ls, ls_out = run(
                    f"git ls-remote --heads origin '{branch_name}' 2>&1",
                    cwd=repo_path
                )
                remote_branch_exists = ok_ls and branch_ref in ls_out

                if remote_branch_exists:
                    plog(f"    ♻️  Branch remota existe — baseando checkout em origin/{branch_name}")
                    ok_co, out_co = run(
                        f"git checkout -B '{branch_name}' 'origin/{branch_name}' 2>&1",
                        cwd=repo_path
                    )
                    if not ok_co:
                        plog(
                            f"    ⚠️  checkout origin/{branch_name} falhou — criando a partir de main: "
                            f"{out_co[:200]}",
                            'warning'
                        )
                        run("git checkout main 2>&1", cwd=repo_path)
                        run("git reset --hard origin/main 2>&1", cwd=repo_path)
                        run(f"git checkout -b '{branch_name}' 2>&1", cwd=repo_path)
                else:
                    run(f"git checkout -b '{branch_name}' 2>&1", cwd=repo_path)

                # C2: src já está dentro do repo clonado — só faz git add
                if str(src.resolve()).startswith(str(Path(repo_path).resolve())):
                    plog(f"    ✅ [C2] Arquivos existentes no repo — fazendo git add")
                    # C2: se output.tf (rastreado) e outputs.tf (novo) coexistem,
                    # remove outputs.tf para evitar Duplicate output definition SEM
                    # fazer git rm de arquivo rastreado (o que o CI interpreta como deleção de recurso).
                    old_out = src / 'output.tf'
                    new_out = src / 'outputs.tf'
                    if old_out.exists() and new_out.exists():
                        plog(f"    🧹 Removendo outputs.tf novo (mantendo output.tf rastreado)")
                        new_out.unlink()
                    # Remove arquivos locais (_ prefix) do diretório antes do git add
                    for local_f in src.glob('_*'):
                        local_f.unlink()
                    _cleanup_local_terraform_artifacts(src)
                    git_add_s3_module(repo_path, rel, dry_run=args.dry_run)
                else:
                    # A/B/C3/C4: copia de mr_output/ para o repo clonado
                    dst = Path(repo_path) / rel
                    dst.mkdir(parents=True, exist_ok=True)
                    copied = 0
                    for item in src.rglob('*'):
                        # Ignora tudo oculto, .terraform e arquivos locais (prefixo _)
                        if any(p.startswith('.') or p == '__pycache__'
                               for p in item.parts):
                            continue
                        if not item.is_file():
                            continue
                        if item.name.startswith('_'):
                            continue
                        rel_item = item.relative_to(src)
                        dst_item = dst / rel_item
                        dst_item.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dst_item)
                        copied += 1
                    # Remove output.tf legado se outputs.tf foi copiado (evita Duplicate output definition)
                    old_out = dst / 'output.tf'
                    new_out = dst / 'outputs.tf'
                    if old_out.exists() and new_out.exists():
                        plog(f"    🧹 Removendo output.tf legado (substituído por outputs.tf)")
                        old_out.unlink()
                    plog(f"    → {copied} arquivo(s) copiado(s)")
                    _cleanup_local_terraform_artifacts(dst)
                    git_add_s3_module(repo_path, rel, dry_run=args.dry_run)

                # Commit
                _ticket_str = f" [{args.ticket}]" if getattr(args, 'ticket', None) else ''
                ok_commit, out_commit = run(
                    f'git commit -m "feat(s3): import {bucket} via blueprint s3{_ticket_str}" 2>&1',
                    cwd=repo_path
                )
                if not ok_commit or 'nothing to commit' in out_commit:
                    plog(f"    ⚠️  Nada para commitar", 'warning')
                    if getattr(args, 'update_mr_descriptions', False):
                        _t, _d, _lb = build_mr_title_desc_labels(bucket, src, rel)
                        if atualizar_mr_gitlab_description(
                            repo_name, branch_name, args.gitlab_url, token, _d, args.dry_run
                        ):
                            plog(f"    📝 Descrição da MR atualizada (sem novo commit)")
                        else:
                            plog(
                                f"    ⚠️  Não foi possível atualizar descrição — "
                                f"MR aberta para `{branch_name}` não encontrada ou erro na API",
                                'warning',
                            )
                    run("git checkout main 2>&1", cwd=repo_path)
                    continue

                # Monta título, labels e descrição (CHANGES.md completo na Overview)
                title, desc, _labels = build_mr_title_desc_labels(bucket, src, rel)

                # Push (confirmação já feita na pré-confirmação em lote)
                ok_push, out_push = run(
                    f"git push origin {branch_name} 2>&1",
                    cwd=repo_path
                )
                if not ok_push:
                    plog(f"    ❌ Push falhou: {out_push[:150]}", 'error')
                    run("git checkout main 2>&1", cwd=repo_path)
                    continue

                # Abre MR (só se ainda não existir — MR vazia anterior ganha diff após o push)
                if existing_open_mr_url:
                    plog(f"    ✅ MR existente atualizada com o push: {existing_open_mr_url}")
                    mr_urls.append({'repo': repo_name, 'bucket': bucket,
                                   'url': existing_open_mr_url, 'branch': branch_name})
                else:
                    url = abrir_mr_gitlab(
                        repo_name, branch_name,
                        args.gitlab_url, token,
                        title, desc, args.dry_run, _labels
                    )
                    plog(f"    ✅ MR aberta: {url}")
                    mr_urls.append({'repo': repo_name, 'bucket': bucket,
                                   'url': url, 'branch': branch_name})

                # Overview da MR = CHANGES.md completo (corrige MRs criadas com descrição curta)
                if atualizar_mr_gitlab_description(
                    repo_name, branch_name, args.gitlab_url, token, desc, args.dry_run
                ):
                    plog(f"    📝 Descrição da MR sincronizada no GitLab (Overview = CHANGES.md)")
                elif not args.dry_run:
                    plog(
                        f"    ⚠️  MR aberta não encontrada ou API falhou — descrição não atualizada",
                        'warning',
                    )

                # Volta para main para o próximo bucket
                run("git checkout main 2>&1", cwd=repo_path)

        else:
            # ── UMA MR POR REPO (comportamento original) ───────────────────────
            branch_name = f"feat/import-s3-{args.env}-{date_tag}"

            if args.dry_run:
                plog(f"  ~ [DRY-RUN] branch={branch_name} → MR com {len(buckets)} buckets")
                mr_urls.append({'repo': repo_name, 'url': 'DRY_RUN',
                               'buckets': len(buckets)})
                continue

            run("git checkout main && git pull --rebase origin main 2>&1", cwd=repo_path)
            run(f"git checkout -b {branch_name} 2>&1", cwd=repo_path)

            for bucket, path in buckets:
                src = Path(path)
                rel = None
                for i, part in enumerate(src.parts):
                    if part == 'services':
                        rel = Path(*src.parts[i:])
                        break
                if not rel:
                    continue
                dst = Path(repo_path) / rel
                dst.mkdir(parents=True, exist_ok=True)
                for f_src in src.glob('*'):
                    # Pula diretórios, arquivos ocultos e arquivos locais (prefixo _)
                    if f_src.is_dir() or f_src.name.startswith('.') or f_src.name.startswith('_'):
                        continue
                    shutil.copy2(f_src, dst / f_src.name)
                # Copia subpastas relevantes (ex: files/policy.json)
                for subdir in src.glob('*/'):
                    if subdir.name.startswith('.') or subdir.name == '.terraform':
                        continue
                    dst_sub = dst / subdir.name
                    dst_sub.mkdir(parents=True, exist_ok=True)
                    for f_src in subdir.glob('*'):
                        if f_src.is_file() and not f_src.name.startswith('_'):
                            shutil.copy2(f_src, dst_sub / f_src.name)
                _cleanup_local_terraform_artifacts(dst)
                git_add_s3_module(repo_path, str(rel), dry_run=args.dry_run)

            # git add já feito por bucket acima
            ok_commit, _ = run(
                f'git commit -m "feat(s3): import {len(buckets)} bucket(s) s3 ({args.env}) via blueprint" 2>&1',
                cwd=repo_path
            )
            if not ok_commit:
                plog(f"  ⚠️  Nada para commitar em {repo_name}", 'warning')
                continue

            ok_push, out_push = run(f"git push origin {branch_name} 2>&1", cwd=repo_path)
            if not ok_push:
                plog(f"  ❌ Push falhou: {out_push[:150]}", 'error')
                continue

            title = f"feat(s3): import buckets s3 ({args.env}) — {len(buckets)} bucket(s)"
            desc  = (
                f"## Import de buckets S3 — {args.env.upper()}\n\n"
                f"**Buckets:** {len(buckets)}\n\n"
                + '\n'.join(f"- `{b}`" for b, _ in buckets)
                + "\n\n---\n*Revisar CHANGES.md de cada bucket antes de aprovar.*"
            )
            url = abrir_mr_gitlab(
                repo_name, branch_name,
                args.gitlab_url, token, title, desc, args.dry_run
            )
            plog(f"  ✅ MR aberta: {url}")
            mr_urls.append({'repo': repo_name, 'url': url, 'buckets': len(buckets)})

    # Resumo das MRs
    plog(f"\n{'='*65}")
    plog(f"  MRs abertas: {len(mr_urls)}")
    if args.one_mr_per_bucket:
        for mr in mr_urls:
            plog(f"    {mr.get('bucket','?')} → {mr['url']}")
    else:
        for mr in mr_urls:
            plog(f"    {mr['repo']} ({mr.get('buckets','?')} buckets) → {mr['url']}")
    plog("")
    return mr_urls


def main():
    parser = argparse.ArgumentParser(description='Orquestrador de migração S3')
    parser.add_argument('--csv',           required=True)
    parser.add_argument('--env',           choices=['dev','hml','prd','all'], default='dev')
    parser.add_argument('--confirm-prd',   action='store_true')
    parser.add_argument('--approve',       action='store_true',
                        help='Executa de verdade. Sem isso é sempre dry-run.')
    parser.add_argument('--skip-no-owner', action='store_true', default=True)
    parser.add_argument('--team',          default=None)
    parser.add_argument('--bucket',        default=None)
    parser.add_argument('--repos-dir',     default='./repos')
    parser.add_argument('--lifecycle-dir', default='./lifecycles')
    parser.add_argument('--output-dir',    default='./mr_output')
    parser.add_argument('--parallel',      type=int, default=5)
    parser.add_argument('--gitlab-url',    default=DEFAULT_GITLAB_URL)
    parser.add_argument('--gitlab-token',  default=None)
    parser.add_argument('--state-bucket',  default=DEFAULT_STATE_BUCKET)
    parser.add_argument('--state-region',  default=DEFAULT_STATE_REGION)
    parser.add_argument('--skip-blockers', action='store_true', default=False,
                        help='Ignora buckets com qualquer blocker (processa só os 100%% prontos).')
    parser.add_argument('--one-mr-per-bucket', action='store_true', default=False,
                        help='Abre uma MR por bucket (mais seguro). Padrão: uma MR por repo de time.')
    parser.add_argument('--update-mr-descriptions', action='store_true', default=False,
                        help='Modo 1 MR/bucket: após push, sincroniza a descrição da MR no GitLab '
                             '(Overview) com o CHANGES.md completo de mr_output. Se não houver '
                             'commit, com esta flag ainda tenta só o PUT da descrição.')
    parser.add_argument('--ticket', default=None,
                        help='Número do ticket desta MR (ex: SRE-1234). '
                             'Obrigatório para --approve.')
    parser.add_argument('--dry-run-full', action='store_true', default=False,
                        help='Gera todos os arquivos localmente (extrai da AWS, cria main.tf etc) '
                             'mas NÃO faz push, NÃO abre MRs e NÃO copia tfstate. '
                             'Use para inspecionar os arquivos antes do --approve.')
    parser.add_argument('--state-mv', action='store_true', default=False,
                        help='C3/C4: copia tfstate do path legado para o novo repo. '
                             'PERIGOSO com Blueprint S3 (endereços module.s3.module.s3). '
                             'Padrão: import limpo (apaga state destino + Fase 3 import).')
    parser.add_argument('--skip-phase',    nargs='+', default=[],
                        choices=['lifecycle','gerar','plan','mrs','state_mv'],
                        help='Pula fases específicas (state_mv = não toca tfstate C3/C4)')
    parser.add_argument('--auto-confirm',  action='store_true', default=False,
                        help='Pula confirmação interativa por bucket no --approve '
                             '(equivalente ao comportamento de CI). '
                             'Use quando processar muitos buckets em lote.')
    parser.add_argument('--max-cache-age', type=int, default=None, metavar='HORAS',
                        help='Re-extrai configs da AWS se o cache tiver mais de N horas. '
                             'Padrão: sem limite (usa cache existente). '
                             'Recomendado: 24 para HML, 4 para PRD.')
    parser.add_argument('--exclude', nargs='+', default=[], metavar='BUCKET',
                        help='Excluir estes buckets da abertura de MR (passa-se um ou mais nomes).')

    args = parser.parse_args()
    # --dry-run-full: executa fases 1 e 2 de verdade (extrai AWS, gera arquivos)
    # mas pula fases 2b (state mv), 3 (plan) e 4 (MRs)
    if args.dry_run_full:
        args.dry_run = False       # deixa fases 1 e 2 rodarem de verdade
        args.skip_phase = list(set(getattr(args, 'skip_phase', []) + ['plan', 'mrs']))
        plog("  Modo: DRY-RUN-FULL — gera arquivos localmente, SEM push/MR/state mv")
    else:
        args.dry_run = not args.approve

    plog("=" * 65)
    plog("  S3 MIGRATE — Orquestrador único de migração")
    plog("=" * 65)
    plog(f"  Modo:         {'DRY-RUN' if args.dry_run else '🚀 APPLY'}")
    plog(f"  CSV:          {args.csv}")
    plog(f"  Env:          {args.env}")
    plog(f"  Skip no-owner:{args.skip_no_owner}")
    plog(f"  Output:       {args.output_dir}")
    plog(f"  Repos dir:    {args.repos_dir}")
    plog("=" * 65)
    plog("")

    # Fase 0 — Validação
    token = fase0_validacao(args)

    # Carrega CSV
    rows = load_csv(args.csv)
    plog(f"  CSV carregado: {len(rows)} linhas")

    # Filtra
    selected, skipped = filter_buckets(rows, args)
    plog(f"  Selecionados:  {len(selected)}")
    plog(f"  Ignorados:     {len(skipped)} (sem owner ou blocker crítico)")

    # Salva skipped
    if skipped:
        skip_path = Path(args.output_dir)
        skip_path.mkdir(parents=True, exist_ok=True)
        with open(skip_path / 'skipped.csv', 'w', newline='') as f:
            if skipped:
                writer = csv.DictWriter(f, fieldnames=skipped[0].keys(), delimiter=';')
                writer.writeheader()
                writer.writerows(skipped)
        plog(f"  Skipped salvo em: {args.output_dir}/skipped.csv")

    if not selected:
        plog("  Nenhum bucket para processar. Encerrando.", 'warning')
        sys.exit(0)

    plog("")

    # Separação por tipo de operação
    cats_lc = ['B','C2','C4']
    cats_legado = ['C3','C4']

    buckets_lc     = [r for r in selected if r.get('category','') in cats_lc]
    buckets_legado = [r for r in selected if r.get('category','') in cats_legado]

    # Fase 1 — Lifecycle
    if 'lifecycle' not in args.skip_phase and buckets_lc:
        fase1_lifecycle(buckets_lc, args.lifecycle_dir, args)

    # Fase 2 — Geração
    if 'gerar' not in args.skip_phase:
        bucket_paths, bucket_mr_flags = fase2_gerar_todos(selected, args.lifecycle_dir, args.output_dir, args)
    else:
        bucket_paths = {}
        bucket_mr_flags = {}

    # Fase 2b — State mv (C3/C4)
    # dry-run-full não faz state mv
    if getattr(args, 'dry_run_full', False):
        legacy_states = set()
        if buckets_legado:
            plog("  [DRY-RUN-FULL] State mv pulado — rode com --approve para executar")
    elif ('gerar' not in args.skip_phase
          and 'state_mv' not in args.skip_phase
          and buckets_legado):
        legacy_states = fase2b_state_mv(buckets_legado, bucket_paths, args) or set()
    elif 'state_mv' in args.skip_phase and buckets_legado:
        plog("  FASE 2b pulada (--skip-phase state_mv) — tfstate remoto preservado")
        legacy_states = set()
    else:
        legacy_states = set()

    # Fase 3 — Terraform plan
    if 'plan' not in args.skip_phase and bucket_paths:
        plan_results, blocked = fase3_terraform_plan(bucket_paths, args.repos_dir, args,
                                                         legacy_states=legacy_states)
    else:
        plan_results = [{'bucket': b, 'status': 'DRY_RUN', 'issues': []}
                        for b in bucket_paths]
        blocked = []

    # Gate: se houver bloqueados e não for dry-run, para
    if blocked and not args.dry_run:
        plog(f"\n  🔴 {len(blocked)} bucket(s) bloqueados no plan.", 'error')
        plog(f"     Revise o plan_report.md, corrija e rode novamente.", 'error')
        plog(f"     Para pular a fase de plan: --skip-phase plan", 'error')
        sys.exit(1)

    # Fase 4 — MRs
    if 'mrs' not in args.skip_phase and bucket_paths:
        mrs = fase4_mrs(bucket_paths, plan_results, args, token, bucket_mr_flags=bucket_mr_flags)

    # Resumo final
    plog("=" * 65)
    plog("  CONCLUÍDO")
    plog("=" * 65)
    plog(f"  Processados:  {len(selected)}")
    plog(f"  Ignorados:    {len(skipped)}")
    if 'plan' not in args.skip_phase:
        plog(f"  Plan OK:      {sum(1 for r in plan_results if r['status'] in ('OK','DRY_RUN'))}")
        plog(f"  Bloqueados:   {len(blocked)}")
    if args.dry_run:
        plog(f"\n  ✅ Dry-run concluído. Para executar de verdade: --approve")
    plog("=" * 65)

if __name__ == '__main__':
    main()






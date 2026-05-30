#!/usr/bin/env python3
"""
s3_preflight.py v2 — Discovery completo antes da migração

Detecta 24 cenários de risco em 4 categorias:
1. tfstate (versão, corrompido, vazio, lock, multi-bucket)
2. Configurações AWS não suportadas pela BP
3. Infraestrutura / permissões
4. Dados do CSV inconsistentes

Uso:
python3 s3_preflight.py --csv levantamento_completo.csv --team lno --env dev
python3 s3_preflight.py --csv levantamento_completo.csv --all --parallel 12
"""

import argparse, csv, json, os, re, subprocess, sys, threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATE_BUCKET = "387979423286-tfstate"
_lock = threading.Lock()

def log(msg):
    with _lock: print(msg, flush=True)

def aws(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip(), r.stderr.strip()

# ══════════════════════════════════════════════════════════════════
# CHECKS DE TFSTATE
# ══════════════════════════════════════════════════════════════════

def check_tfstate(key, bucket_name):
    issues = []
    if not key or key in ('nan', '', 'None'):
        return issues

    # Verifica existência
    ok, _, _ = aws(f"aws s3api head-object --bucket {STATE_BUCKET} --key '{key}' 2>/dev/null")
    if not ok:
        return issues # não existe — sem problema, import limpo

    # Verifica lock ativo
    lock_key = key + '.tflock'
    ok_lock, _, _ = aws(f"aws s3api head-object --bucket {STATE_BUCKET} --key '{lock_key}' 2>/dev/null")
    if ok_lock:
        issues.append({
            'tipo': 'TFSTATE_LOCK_ATIVO', 'sev': 'CRITICO',
            'msg': f"tflock ativo em {lock_key} — outro processo pode estar rodando",
            'fix': f"aws s3 rm s3://{STATE_BUCKET}/{lock_key}",
        })

    # Baixa o state
    ok_get, raw, _ = aws(f"aws s3 cp s3://{STATE_BUCKET}/{key} - 2>/dev/null")
    if not ok_get or not raw:
        issues.append({
            'tipo': 'TFSTATE_VAZIO', 'sev': 'ALTO',
            'msg': "State existe mas está vazio — será feito import limpo",
            'fix': f"aws s3 rm s3://{STATE_BUCKET}/{key}",
        })
        return issues

    # Parse JSON
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        issues.append({
            'tipo': 'TFSTATE_CORROMPIDO', 'sev': 'CRITICO',
            'msg': f"State com JSON inválido: {e}",
            'fix': f"aws s3 rm s3://{STATE_BUCKET}/{key} # e fazer import limpo",
        })
        return issues

    # Verifica versão do Terraform
    tf_version = state.get('terraform_version', '0.0.0')
    resources = state.get('resources', [])
    modules = state.get('modules', []) # formato 0.11

    if modules and not resources:
        issues.append({
            'tipo': 'TFSTATE_0_11', 'sev': 'CRITICO',
            'msg': f"State em formato Terraform 0.11 (campo 'modules') — incompatível",
            'fix': f"aws s3 rm s3://{STATE_BUCKET}/{key} # import limpo obrigatório",
        })
        return issues

    # Providers legados (0.12)
    legacy_providers = set()
    for res in resources:
        prov = res.get('provider', '')
        if prov and 'registry.terraform.io' not in prov and 'provider.' in prov:
            legacy_providers.add(prov)

    if legacy_providers:
        issues.append({
            'tipo': 'TFSTATE_LEGADO', 'sev': 'ALTO',
            'msg': f"Terraform {tf_version} — providers legados: {', '.join(legacy_providers)}",
            'fix': "Import limpo automático (state legado descartado pelo script)",
        })

    # State com recursos de outros buckets
    # Deduplica por ID real para evitar falso positivo com aws_s3_bucket.main + module.s3.aws_s3_bucket.main
    s3_resources = [r for r in resources if r.get('type') == 'aws_s3_bucket']
    unique_ids = set(
        inst.get('attributes',{}).get('id','')
        for r in s3_resources for inst in r.get('instances',[])
        if inst.get('attributes',{}).get('id')
    )
    # Remove o próprio bucket e strings vazias
    other_ids = {i for i in unique_ids if i and i != bucket_name}
    if other_ids:
        issues.append({
            'tipo': 'TFSTATE_MULTI_BUCKET', 'sev': 'ALTO',
            'msg': f"State gerencia outros buckets além de {bucket_name}: {', '.join(sorted(other_ids))}",
            'fix': "CUIDADO — plan pode propor destroy dos recursos dos outros buckets",
        })

    # state_key incorreto (state existe mas não tem resource do bucket)
    bucket_in_state = any(
        inst.get('attributes', {}).get('id') == bucket_name
        for r in resources if r.get('type') == 'aws_s3_bucket'
        for inst in r.get('instances', [])
    )
    if resources and not bucket_in_state:
        issues.append({
            'tipo': 'TFSTATE_BUCKET_ERRADO', 'sev': 'ALTO',
            'msg': f"State em {key} não contém resource do bucket {bucket_name}",
            'fix': "Verificar state_key_atual no CSV — pode estar apontando para o state errado",
        })

    return issues

# ══════════════════════════════════════════════════════════════════
# CHECKS DE CONFIGURAÇÃO AWS S3
# ══════════════════════════════════════════════════════════════════

def check_s3_config(bucket_name):
    issues = []

    # Região
    ok, out, _ = aws(f"aws s3api get-bucket-location --bucket {bucket_name} --output json 2>/dev/null")
    if not ok:
        issues.append({
            'tipo': 'BUCKET_SEM_ACESSO', 'sev': 'CRITICO',
            'msg': "Sem permissão de acesso ou bucket não existe",
            'fix': "Verificar permissões do role de execução",
        })
        return issues # sem acesso, não adianta continuar

    try:
        region = json.loads(out).get('LocationConstraint') or 'us-east-1'
        if region not in ('us-east-1', None, ''):
            issues.append({
                'tipo': 'BUCKET_REGIAO_DIFERENTE', 'sev': 'ALTO',
                'msg': f"Bucket em {region} — backend.tf precisa de região correta",
                'fix': f"Verificar se o provider no versions.tf tem region = \"{region}\"",
            })
    except Exception:
        pass

    # Ownership controls
    ok, out, _ = aws(f"aws s3api get-bucket-ownership-controls --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            ownership = json.loads(out)['OwnershipControls']['Rules'][0]['ObjectOwnership']
            if ownership != 'BucketOwnerEnforced':
                issues.append({
                    'tipo': 'ACL_CUSTOMIZADA', 'sev': 'MEDIO',
                    'msg': f"ObjectOwnership = {ownership} (BP assume BucketOwnerEnforced)",
                    'fix': "Passar object_ownership no main.tf gerado",
                })
        except Exception:
            pass

    # MFA Delete no versioning
    ok, out, _ = aws(f"aws s3api get-bucket-versioning --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            data = json.loads(out)
            if data.get('MFADelete') == 'Enabled':
                issues.append({
                    'tipo': 'MFA_DELETE', 'sev': 'ALTO',
                    'msg': "MFA Delete habilitado — qualquer alteração de versioning requer MFA",
                    'fix': "Não alterar versioning via Terraform sem desabilitar MFA Delete antes",
                })
        except Exception:
            pass

    # Object Lock
    ok, out, _ = aws(f"aws s3api get-object-lock-configuration --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            if json.loads(out).get('ObjectLockConfiguration', {}).get('ObjectLockEnabled'):
                issues.append({
                    'tipo': 'OBJECT_LOCK', 'sev': 'CRITICO',
                    'msg': "Object Lock habilitado — BP não gerencia, import vai propor destroy",
                    'fix': "Adicionar lifecycle { ignore_changes = [object_lock_configuration] } no main.tf",
                })
        except Exception:
            pass

    # KMS CMK customizada
    ok, out, _ = aws(f"aws s3api get-bucket-encryption --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            rules = json.loads(out)['ServerSideEncryptionConfiguration']['Rules']
            for rule in rules:
                kms_key = rule.get('ApplyServerSideEncryptionByDefault', {}).get('KMSMasterKeyID', '')
                algo = rule.get('ApplyServerSideEncryptionByDefault', {}).get('SSEAlgorithm', '')
                if algo == 'aws:kms' and kms_key and 'alias/aws/s3' not in kms_key:
                    # CMK customizada — verifica se é padrão da BP
                    import re as _re
                    is_bp_default = bool(_re.match(
                        r'alias/[a-z0-9]+-default-(dev|hml|prd)', kms_key
                    ))
                    if not is_bp_default:
                        issues.append({
                            'tipo': 'KMS_CMK_CUSTOMIZADA', 'sev': 'MEDIO',
                            'msg': f"KMS CMK não padrão: {kms_key}",
                            'fix': "Passar kms_master_key_id no main.tf — verificar permissão do CI",
                        })
        except Exception:
            pass

    # Website hosting
    ok, out, _ = aws(f"aws s3api get-bucket-website --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            data = json.loads(out)
            if data.get('IndexDocument'):
                issues.append({
                    'tipo': 'WEBSITE_HOSTING', 'sev': 'MEDIO',
                    'msg': f"Static website habilitado (index: {data['IndexDocument'].get('Suffix','')})",
                    'fix': "BP não gerencia — adicionar lifecycle ignore_changes no main.tf",
                })
        except Exception:
            pass

    # Notifications: Lambda e SNS (além das SQS que já tratamos)
    ok, out, _ = aws(f"aws s3api get-bucket-notification-configuration --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            data = json.loads(out)
            lambdas = data.get('LambdaFunctionConfigurations', [])
            sns = data.get('TopicConfigurations', [])
            if lambdas:
                issues.append({
                    'tipo': 'LAMBDA_NOTIFICATION', 'sev': 'INFO',
                    'msg': f"{len(lambdas)} Lambda notification(s) — extractor gera function_name correto",
                    'fix': "OK — corrigido no extractor v8",
                })
            if sns:
                issues.append({
                    'tipo': 'SNS_NOTIFICATION', 'sev': 'MEDIO',
                    'msg': f"{len(sns)} SNS notification(s) — verificar se BP suporta",
                    'fix': "Inspecionar o main.tf gerado e confirmar que sns_notifications está correto",
                })
        except Exception:
            pass

    # Logging target existe?
    ok, out, _ = aws(f"aws s3api get-bucket-logging --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            data = json.loads(out)
            target = data.get('LoggingEnabled', {}).get('TargetBucket', '')
            if target and target != 'ecs-387979423286-logging-s3':
                ok_target, _, _ = aws(f"aws s3api head-bucket --bucket {target} 2>/dev/null")
                if not ok_target:
                    issues.append({
                        'tipo': 'LOGGING_TARGET_INEXISTENTE', 'sev': 'MEDIO',
                        'msg': f"Logging aponta para {target} que não existe ou sem permissão",
                        'fix': "BP vai alterar o target para o bucket padrão — sem problema",
                    })
        except Exception:
            pass

    # Replication cross-account
    ok, out, _ = aws(f"aws s3api get-bucket-replication --bucket {bucket_name} --output json 2>/dev/null")
    if ok and out:
        try:
            data = json.loads(out)
            rules = data.get('ReplicationConfiguration', {}).get('Rules', [])
            for rule in rules:
                dest_account = rule.get('Destination', {}).get('Account', '')
                if dest_account and dest_account != '387979423286':
                    issues.append({
                        'tipo': 'REPLICATION_CROSS_ACCOUNT', 'sev': 'ALTO',
                        'msg': f"Replicação para conta externa {dest_account}",
                        'fix': "Verificar se o CI tem permissão de alterar replicação cross-account",
                    })
        except Exception:
            pass

    return issues

# ══════════════════════════════════════════════════════════════════
# CHECKS DO CSV
# ══════════════════════════════════════════════════════════════════

def check_csv_consistency(r, all_rows):
    bucket = r.get('bucket_name','').strip()
    team = r.get('team','').strip()
    env = r.get('env','').strip()
    cat = r.get('category','').strip()
    asset = r.get('asset_category','').strip()
    old_key = r.get('state_key_atual','').strip()
    issues = []

    # logical_name fora do padrão
    prefix = f"ecs-{team}-"
    suffix = f"-{env}"
    if bucket.startswith(prefix) and bucket.endswith(suffix):
        logical = bucket[len(prefix):-len(suffix)]
    else:
        logical = re.sub(r'^ecs-', '', bucket)
        issues.append({
            'tipo': 'LOGICAL_NAME_FORA_PADRAO', 'sev': 'MEDIO',
            'msg': f"Nome {bucket} não segue ecs-{{team}}-{{logical}}-{{env}} — logical extraído: '{logical}'",
            'fix': "Verificar se o backend.tf vai gerar o key correto no tfstate",
        })

    # asset_category vazio ou inválido
    valid_cats = {
        'Productive data', 'Model development', 'Metadata', 'Embbeded',
        'Development', 'Staging', 'Sandbox', 'Logs', 'Backup', 'Cache'
    }
    if not asset or asset not in valid_cats:
        issues.append({
            'tipo': 'ASSET_CATEGORY_INVALIDO', 'sev': 'MEDIO',
            'msg': f"asset_category='{asset}' não é um valor válido da BP",
            'fix': f"Valores válidos: {', '.join(sorted(valid_cats))}",
        })

    return issues

# ══════════════════════════════════════════════════════════════════
# SCANNER PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def scan_bucket(r, all_rows):
    bucket = r.get('bucket_name','').strip()
    team = r.get('team','').strip()
    env = r.get('env','').strip()
    cat = r.get('category','').strip()
    old_key = r.get('state_key_atual','').strip()

    all_issues = []

    # 1. tfstate legado/problemático
    all_issues += check_tfstate(old_key, bucket)

    # tfstate no NOVO path (pode já ter sido copiado errado)
    logical = bucket
    prefix = f"ecs-{team}-"; suffix = f"-{env}"
    if bucket.startswith(prefix) and bucket.endswith(suffix):
        logical = bucket[len(prefix):-len(suffix)]
    new_key = f"ecs-{team}-default-aws-terraform/services/s3/{logical}/{env}/terraform.tfstate"
    new_issues = check_tfstate(new_key, bucket)
    for i in new_issues:
        i['tipo'] += '_NOVO_PATH'
        if i['sev'] == 'ALTO': i['sev'] = 'CRITICO'
        i['msg'] = f"[NO NOVO PATH] {i['msg']}"
    all_issues += new_issues

    # 2. Configuração AWS
    all_issues += check_s3_config(bucket)

    # 3. CSV consistency
    all_issues += check_csv_consistency(r, all_rows)

    return bucket, team, env, cat, all_issues

# ══════════════════════════════════════════════════════════════════
# RELATÓRIO
# ══════════════════════════════════════════════════════════════════

SEV_ORDER = {'CRITICO': 0, 'ALTO': 1, 'MEDIO': 2, 'BAIXO': 3, 'INFO': 4}
SEV_ICONS = {'CRITICO': '🔴', 'ALTO': '🟠', 'MEDIO': '🟡', 'BAIXO': '🟢', 'INFO': 'ℹ️ '}

def print_report(resultados, output_file='preflight_report.json'):
    total = len(resultados)
    com_issue = sum(1 for *_, issues in resultados if issues)
    by_type = defaultdict(list)
    by_sev = defaultdict(int)

    for bucket, team, env, cat, issues in resultados:
        for issue in issues:
            by_type[issue['tipo']].append({**issue, 'bucket': bucket, 'team': team,
            'env': env, 'cat': cat})
            by_sev[issue['sev']] += 1

    print(f"\n{'='*65}")
    print(f" PRE-FLIGHT SCAN — Resultado")
    print(f"{'='*65}")
    print(f" Scaneados: {total} | Com issues: {com_issue}")
    for sev in ['CRITICO', 'ALTO', 'MEDIO', 'INFO']:
        if by_sev[sev]:
            print(f" {SEV_ICONS[sev]} {sev}: {by_sev[sev]}")
    print(f"{'='*65}\n")

    fix_cmds = []
    for tipo, items in sorted(by_type.items(),
            key=lambda x: SEV_ORDER.get(x[1][0]['sev'], 9)):
        sev = items[0]['sev']
        icon = SEV_ICONS.get(sev, '?')
        print(f"{icon} [{sev}] {tipo} — {len(items)} bucket(s)")
        for item in items[:10]: # limita a 10 por tipo
            print(f"  • {item['bucket']} [{item['cat']}] ({item['team']}/{item['env']})")
            print(f"    {item['msg']}")
            print(f"    fix: {item.get('fix','')}")
            if 'aws s3 rm' in item.get('fix','') or 'aws s3api' in item.get('fix',''):
                fix_cmds.append(item['fix'])
        if len(items) > 10:
            print(f"  ... +{len(items)-10} mais")
        print()

    if fix_cmds:
        print(f"{'='*65}")
        print(f" Comandos de correção (críticos):")
        print(f"{'='*65}")
        for cmd in sorted(set(fix_cmds)):
            print(f"  {cmd}")
        # Salva script de correção
        fix_path = Path('preflight_fix.sh')
        fix_path.write_text('#!/bin/bash\n' + '\n'.join(sorted(set(fix_cmds))) + '\n')
        fix_path.chmod(0o755)
        print(f"\n Script de correção salvo em: {fix_path}")
    print()

    # JSON
    output = {'total': total, 'com_issues': com_issue, 'by_sev': dict(by_sev),
        'resultados': [{'bucket': b, 'team': t, 'env': e, 'category': c, 'issues': iss}
        for b, t, e, c, iss in resultados]}
    Path(output_file).write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f" Relatório JSON: {output_file}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', required=True)
    parser.add_argument('--team', default=None)
    parser.add_argument('--env', default='dev')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--parallel', type=int, default=6)
    parser.add_argument('--output', default='preflight_report.json')
    args = parser.parse_args()

    # Carrega CSV
    for enc in ['utf-8-sig', 'utf-8', 'latin-1']:
        try:
            with open(args.csv, encoding=enc) as f:
                raw = f.read().lstrip('﻿')
            sep = ';' if raw.split('\n')[0].count(';') > raw.split('\n')[0].count(',') else ','
            rows = [{k.strip(): (v.strip() if v else '') for k, v in r.items() if k}
                for r in csv.DictReader(raw.splitlines(), delimiter=sep)]
            break
        except Exception:
            rows = None

    if not rows:
        print("Erro ao carregar CSV"); sys.exit(1)

    selected = [r for r in rows
        if r.get('bucket_name','').strip()
        and r.get('team','').strip() not in ('nan','None','')
        and (args.all or (
            (not args.team or r.get('team','').strip() == args.team) and
            r.get('env','').strip() == args.env
        ))]

    # Verifica colisões de logical_name no CSV
    seen = defaultdict(list)
    for r in selected:
        team = r.get('team',''); env = r.get('env','')
        bucket = r.get('bucket_name','')
        prefix = f"ecs-{team}-"; suffix = f"-{env}"
        logical = bucket[len(prefix):-len(suffix)] if bucket.startswith(prefix) and bucket.endswith(suffix) else bucket
        seen[(team, logical, env)].append(bucket)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    if collisions:
        print(f"\n⚠️ COLISÕES DE LOGICAL_NAME DETECTADAS:")
        for (team, logical, env), buckets in collisions.items():
            print(f"  ({team}, {logical}, {env}): {buckets}")
        print()

    print(f"\n Scaneando {len(selected)} buckets ({args.parallel} workers)...")
    print(f" Checks: tfstate (versão/lock/corrompido/multi-bucket) + "
        f"S3 config (região/ACL/KMS/lambda/sns/object-lock) + CSV\n")

    resultados = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {ex.submit(scan_bucket, r, rows): r for r in selected}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            resultados.append(result)
            bucket, _, _, _, issues = result
            sev_max = min((SEV_ORDER.get(i['sev'], 9) for i in issues), default=5)
            icon = ['🔴','🟠','🟡','🟢','ℹ️ ','✅'][min(sev_max, 5)]
            log(f" [{done:>3}/{len(selected)}] {icon} {bucket} — {len(issues)} issue(s)")

    print_report(resultados, args.output)

if __name__ == '__main__':
    main()


_DOCS = """
readme:


# Migrar S3 — tooling de discovery e migração para blueprint

Scripts para **discovery**, **geração de Terraform** e **orquestração de MRs** na migração de buckets S3 legados para a blueprint
[`ecs-engineering-terraform-blueprint-aws-s3`](https://gitlab.ecsbr.net/ecs/engineering/ecs-engineering-terraform-blueprint-aws-s3).

Origem: projeto interno `discoverys3` (ondas DEV/HML/PRD).

## Pré-requisitos

- Python 3.9+
- AWS CLI configurado (`aws sts get-caller-identity`)
- Terraform >= 1.5
- `GITLAB_TOKEN` (para abrir MRs)
- CSV de inventário de buckets (`levantamento_completo.csv`) — **não versionado**; gerar/obter localmente

## Scripts

| Arquivo | Função |
|---------|--------|
| `s3_discovery.py` | Discovery unificado — preflight + extract + tier AUTO/REVIEW/BLOCK |
| `s3_preflight.py` | Checks rápidos (tfstate, ACL, KMS, replication) |
| `s3_config_extractor.py` | Extrai config AWS por bucket -> JSON |
| `s3_main_tf_gen.py` | Gera `main.tf`, imports, `CHANGES.md` a partir do JSON |
| `s3_migrate.py` | **Orquestrador** — generate, plan, import, MR no GitLab |
| `check_sqs_kms_policies.py` | Audit SQS + SSE-KMS (`kms_keys` na queue policy) |
| `regen_mr_docs.py` | Regenera documentação de MRs |

## Fluxo recomendado

### 1. Discovery (antes de qualquer onda)

```bash
cd migrar-s3
aws sts get-caller-identity

python3 s3_discovery.py \\
--csv levantamento_completo.csv \\
--env dev \\
--extract \\
--parallel 8 \\
--output-prefix discovery_dev
```

Saída em `discovery_output/` (gitignored):

- `discovery_dev.csv` — filtrar por coluna `tier`
- `discovery_dev_summary.md`
- `.s3configs/*.json` — cache usado no generate

Detalhes: [`docs/DISCOVERY-HML-PRD.md`](docs/DISCOVERY-HML-PRD.md)

### 2. Generate (dry-run)

```bash
export GITLAB_TOKEN="..."

python3 s3_migrate.py \\
--csv levantamento_completo.csv \\
--env dev \\
--team platform \\
--ticket SREK-XXXX \\
--dry-run-full
```

Artefatos em `mr_output/ecs-{team}-default-aws-terraform/services/s3/{svc}/{env}/`

### 3. Validate + MR

Use um script de onda como referência: [`docs/examples/onda5-dev.sh`](docs/examples/onda5-dev.sh)

```bash
# Exemplo: copiar e adaptar lista de buckets/times
cp docs/examples/onda5-dev.sh ./minha-onda-dev.sh
# editar BUCKETS, TEAMS, TICKET
./minha-onda-dev.sh generate
./minha-onda-dev.sh regen-imports
./minha-onda-dev.sh validate-one id ecs-id-events-dev
./minha-onda-dev.sh validate
./minha-onda-dev.sh mr
```

### 4. SQS + SSE-KMS

Se o bucket notifica fila criptografada com CMK:

```bash
python3 check_sqs_kms_policies.py --help
```

O gerador emite `kms_keys` na `sqs_notifications` quando detecta statement `KMSAllows` na queue policy (requer blueprint S3 com suporte `kms_keys`).

## Critérios de plan (resumo)

| Resultado | Acao |
|-----------|------|
| `0 add`, `N change`, `0 destroy` | MR |
| `+1`/`+2` (lifecycle, ownership, lambda_permission) | MR + documentar em `CHANGES.md` |
| **destroy** no bucket | Parar — revisar state/import |
| drift lifecycle 90d->GLACIER_IR | Validar com time antes do merge |

## Diretórios locais (gitignored)

| Dir | Conteúdo |
|-----|----------|
| `repos/` | clones GitLab dos repos de time |
| `mr_output/` | Terraform gerado + plans |
| `lifecycles/` | JSONs de lifecycle extraídos |
| `discovery_output/` | relatórios e `.s3configs/` |

## Blueprint

```hcl
# ref usada pelo gerador (ajustar em s3_migrate.py / s3_main_tf_gen.py se necessário)
BP_SOURCE = "git::https://gitlab.ecsbr.net/ecs/engineering/ecs-engineering-terraform-blueprint-aws-s3.git//default?ref=2"
```


tem uma pasta chamada docs dentro desse repo
DISCOVERY-HML-PRD.md

# Discovery antes das ondas (HML / PRD)

## Objetivo

Rodar **uma vez por ambiente** antes de `generate` / ondas, para classificar buckets sem abrir MR as cegas.

Ferramentas:

| Script | O que faz |
|--------|-----------|
| `s3_discovery.py` | **Principal** — preflight + extract + flags do gerador + tier AUTO/REVIEW/BLOCK |
| `s3_preflight.py` | Só tfstate + checks AWS rápidos (sem lifecycle/SQS KMS) |
| `check_sqs_kms_policies.py` | Audit fino só SQS/KMS (após ter `sqs_notifications` no config) |

## Comando recomendado

```bash
cd /Users/C92519A/core/discoverys3/script
aws sts get-caller-identity

# HML — extrai configs e gera relatório (~paralelo 8)
python3 s3_discovery.py \\
--csv levantamento_completo.csv \\
--env hml \\
--extract \\
--parallel 8 \\
--output-prefix discovery_hml

# PRD
python3 s3_discovery.py \\
--csv levantamento_completo.csv \\
--env prd \\
--extract \\
--parallel 8 \\
--output-prefix discovery_prd
```

Saída em `discovery_output/`:

- `discovery_hml.csv` — filtrar no Excel/Sheets por coluna `tier`
- `discovery_hml_summary.md` — visão agregada
- `discovery_hml.json` — detalhe por bucket (issues completas)
- `.s3configs/*.json` — cache reutilizado no `generate` (copiar ou apontar `--configs-dir`)

## Tiers (como montar ondas)

| Tier | Significado | Acao na onda |
|------|-------------|--------------|
| **BLOCK** | CSV `blockers`, object lock, tfstate 0.11/multi-bucket, sem acesso | **Nao** incluir no `onda*.sh` até limpar |
| **REVIEW** | LC custom/drift, replication, SQS KMS, AES256, SNS, fila morta | Lote menor + `validate-one` + CHANGES |
| **AUTO** | Só drift "aceitável" (tags, logging format) com BP 2.2.1 | Candidato a lote maior automatizado |

## Flags do script (`script_flags`)

| Flag | Script resolve | Time decide |
|------|----------------|-------------|
| `LC_BP_NEW` | BP cria LC | Se negócio aceita retenção padrão |
| `LC_DRIFT` | BP 2.2.1 ou `lifecycle_rules` no HCL | Manter Glacier ou não |
| `LC_CUSTOM` | Emite `lifecycle_rules` | Validar regra a regra |
| `SQS_KMS` | Emite `kms_keys` | — |
| `SQS_DEAD` | Corrigir ARN antes do generate | — |
| `REPLICATION` | `replication_rules` no HCL | Destino/role |
| `AES256` | `sse_algorithm` explícito | — |
| `SNS_UNTESTED` | Mapeia SNS | Plan sem destroy |

## Pré-requisitos antes do generate em HML/PRD

1. BP tag **`2.2.1`** em `s3_migrate.py` -> `BP_SOURCE`
2. Discovery **BLOCK** = 0 (ou lista explícita de exceções)
3. Canários: 1x `AUTO`, 1x `REVIEW` com `LC_DRIFT`, 1x `REPLICATION` se houver
4. `check_sqs_kms_policies.py` após generate dos que têm SQS

## O que não precisa mais fazer manual por bucket

- Varredura de object lock / website / inventory (discovery marca `NOT_SUPPORTED`)
- Achar fila SQS com KMS (flag `SQS_KMS`)
- Classificar lifecycle custom vs BP (coluna `lc_mode`)
- tfstate legado / multi-bucket (preflight embutido)

## O que continua manual (time dono)

- AppID / tags de negócio
- Aceitar ou não remoção de transição Glacier quando `LC_DRIFT`
- Merge e janela de apply


/examples

onda5-dev.sh


#!/usr/bin/env bash
# Onda 5 DEV — id (7) + partnerportal (9) = 16 buckets -> acum. 77
# Pré-requisito: export GITLAB_TOKEN=... && aws sts get-caller-identity OK
# Uso:
# ./onda5-dev.sh generate
# ./onda5-dev.sh regen-imports
# ./onda5-dev.sh validate
# ./onda5-dev.sh validate-one id ecs-id-easyflow-dev
# ./onda5-dev.sh mr

set -euo pipefail
cd "$(dirname "$0")"

CSV=levantamento_completo.csv
TICKET=SREK-8432
STATE_BUCKET=387979423286-tfstate
TEAMS=(id partnerportal)

# team|bucket|repo_team|svc (logical services/s3/<svc>/dev)
BUCKETS=(
"id|ecs-id-datalake-sync-dev|id|datalake-sync"
"id|ecs-id-easyflow-dev|id|easyflow"
"id|ecs-id-events-dev|id|events"
"id|ecs-id-partner-hub-dev|id|partner-hub"
"id|ecs-id-partner-hub-logs-siem-dev|id|partner-hub-logs-siem"
"id|ecs-id-shared-dev|id|shared"
"id|ecs-id-user-terms-dev|id|user-terms"
"partnerportal|ecs-partnerportal-blocklist-fileprocessor-dev|partnerportal|blocklist-fileprocessor"
"partnerportal|ecs-partnerportal-breach-installments-report-dev|partnerportal|breach-installments-report"
"partnerportal|ecs-partnerportal-conciliation-reports-dev|partnerportal|conciliation-reports"
"partnerportal|ecs-partnerportal-debts-reports-dev|partnerportal|debts-reports"
"partnerportal|ecs-partnerportal-events-dev|partnerportal|events"
"partnerportal|ecs-partnerportal-installment-report-dev|partnerportal|installment-report"
"partnerportal|ecs-partnerportal-partner-hub-dev|partnerportal|partner-hub"
"partnerportal|ecs-partnerportal-testebiab-dev|partnerportal|testebiab"
"partnerportal|ecs-partnerportal-tutorial-registers-manager-dev|partnerportal|tutorial-registers-manager"
)

migrate() {
local team=$1
shift
python3 s3_migrate.py --csv "$CSV" --env dev --team "$team" --ticket "$TICKET" \\
--one-mr-per-bucket --skip-blockers "$@"
}

validate_bucket() {
local repo_team=$1 bucket=$2 svc=$3
local key="ecs-${repo_team}-default-aws-terraform/services/s3/${svc}/dev/terraform.tfstate"
local dir="mr_output/ecs-${repo_team}-default-aws-terraform/services/s3/${svc}/dev"

echo "======== ${bucket} ========"
echo "path: ${dir}"
if [[ ! -f "$dir/main.tf" ]]; then
echo "ERRO: main.tf ausente em ${dir} (rode: ./onda5-dev.sh generate)"
return 1
fi

aws s3 rm "s3://${STATE_BUCKET}/${key}" 2>/dev/null || true
(
cd "$dir"
rm -rf .terraform
terraform init -reconfigure -input=false
bash _import_commands.sh
echo "--- state ---"
terraform state list | grep '^module\\.s3\\.aws_s3' || terraform state list | head -25
dup=$(terraform state list 2>/dev/null | grep -c 'aws_s3_bucket\\.main\\[0\\]' || true)
if [[ "$dup" -gt 0 ]]; then
echo "ERRO: aws_s3_bucket.main[0] no state"
exit 1
fi
echo "--- plan ---"
terraform plan -no-color | grep -E '^Plan:|will be created|will be destroyed' || true
)
}

cmd_generate() {
for t in "${TEAMS[@]}"; do
echo ">>> dry-run-full: $t"
migrate "$t" --dry-run-full --skip-phase mrs
done
echo "Artefatos em mr_output/ecs-{id,partnerportal}-default-aws-terraform/services/s3/"
}

cmd_validate() {
for row in "${BUCKETS[@]}"; do
IFS='|' read -r _team bucket repo_team svc <<< "$row"
validate_bucket "$repo_team" "$bucket" "$svc" || true
echo
done
}

cmd_validate_one() {
local team=$1 bucket=$2
for row in "${BUCKETS[@]}"; do
IFS='|' read -r t b rt svc <<< "$row"
if [[ "$t" == "$team" && "$b" == "$bucket" ]]; then
validate_bucket "$rt" "$b" "$svc"
return
fi
done
echo "Bucket não está na onda 5: $team $bucket"
exit 1
}

cmd_regen_imports() {
python3 << 'PY'
import json
from pathlib import Path
from s3_main_tf_gen import gen_import_commands

BUCKETS = [
("id", "ecs-id-datalake-sync-dev", "id", "datalake-sync"),
("id", "ecs-id-easyflow-dev", "id", "easyflow"),
("id", "ecs-id-events-dev", "id", "events"),
("id", "ecs-id-partner-hub-dev", "id", "partner-hub"),
("id", "ecs-id-partner-hub-logs-siem-dev", "id", "partner-hub-logs-siem"),
("id", "ecs-id-shared-dev", "id", "shared"),
("id", "ecs-id-user-terms-dev", "id", "user-terms"),
("partnerportal", "ecs-partnerportal-blocklist-fileprocessor-dev", "partnerportal", "blocklist-fileprocessor"),
("partnerportal", "ecs-partnerportal-breach-installments-report-dev", "partnerportal", "breach-installments-report"),
("partnerportal", "ecs-partnerportal-conciliation-reports-dev", "partnerportal", "conciliation-reports"),
("partnerportal", "ecs-partnerportal-debts-reports-dev", "partnerportal", "debts-reports"),
("partnerportal", "ecs-partnerportal-events-dev", "partnerportal", "events"),
("partnerportal", "ecs-partnerportal-installment-report-dev", "partnerportal", "installment-report"),
("partnerportal", "ecs-partnerportal-partner-hub-dev", "partnerportal", "partner-hub"),
("partnerportal", "ecs-partnerportal-testebiab-dev", "partnerportal", "testebiab"),
("partnerportal", "ecs-partnerportal-tutorial-registers-manager-dev", "partnerportal", "tutorial-registers-manager"),
]
base = Path("mr_output")
cfg_dir = base / ".s3configs"
for team, bucket, repo_team, svc in BUCKETS:
cfg_path = cfg_dir / f"{bucket}.s3config.json"
if not cfg_path.exists():
print(f"SKIP {bucket}: sem config (rode generate)")
continue
cfg = json.loads(cfg_path.read_text())
ac = (cfg.get("tags") or {}).get("Asset_Category") or (cfg.get("tags") or {}).get("asset_category") or "Development"
repo = f"ecs-{repo_team}-default-aws-terraform"
state_key = f"{repo}/services/s3/{svc}/dev/terraform.tfstate"
out = base / repo / "services/s3" / svc / "dev"
out.mkdir(parents=True, exist_ok=True)
(out / "_import_commands.sh").write_text(
gen_import_commands(bucket, cfg, state_key, env="dev", asset_cat=ac),
encoding="utf-8",
)
print(f"OK {bucket}")
PY
}

cmd_mr() {
for t in "${TEAMS[@]}"; do
echo ""
read -r -p "Abrir MRs para time ${t}? [s/N] " ans
case "$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')" in
s|sim|y|yes) migrate "$t" --approve --skip-phase lifecycle plan state_mv ;;
esac
done
}

case "${1:-}" in
generate) cmd_generate ;;
regen-imports) cmd_regen_imports ;;
validate) cmd_validate ;;
validate-one) cmd_validate_one "${2:?team}" "${3:?bucket}" ;;
mr) cmd_mr ;;
*)
echo "Uso: $0 {generate|regen-imports|validate|validate-one TEAM BUCKET|mr}"
exit 1
;;
esac
"""

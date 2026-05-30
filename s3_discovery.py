#!/usr/bin/env python3
"""
s3_discovery.py — Discovery unificado antes das ondas (DEV / HML / PRD)

Combina:
- s3_preflight.py (tfstate, ACL, object lock, KMS, SNS, replication, CSV)
- s3_config_extractor (extract AWS + configs não suportadas pela BP)
- heurísticas do gerador (lifecycle custom/drift, SQS KMS, filas inexistentes)

Saída:
- discovery_report.json
- discovery_report.csv (filtro por tier / env para planejar ondas)
- discovery_summary.md

Uso:
cd script
python3 s3_discovery.py --csv levantamento_completo.csv --env hml --parallel 8
python3 s3_discovery.py --csv levantamento_completo.csv --env prd --all --extract
python3 s3_discovery.py --csv levantamento_completo.csv --env dev --team platform --tier REVIEW
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Imports locais do projeto
from s3_config_extractor import (
    extract_bucket,
    get_kms_key_id,
    get_not_supported,
    has_notifications,
    kms_keys_from_queue_policy_document,
    load_config,
    fetch_sqs_queue_policy_document,
)
from s3_main_tf_gen import (
    lifecycle_matches_bp_default,
    lifecycle_transition_drift_warnings,
)
from s3_preflight import SEV_ORDER, SEV_ICONS, scan_bucket

DEFAULT_CONFIGS = SCRIPT_DIR / 'discovery_output' / '.s3configs'
DEFAULT_OUT_DIR = SCRIPT_DIR / 'discovery_output'

TIER_ORDER = {'BLOCK': 0, 'REVIEW': 1, 'AUTO': 2}

# Tipos preflight / discovery que impedem onda automática
BLOCK_TIPOS = {
    'TFSTATE_0_11', 'TFSTATE_CORROMPIDO', 'TFSTATE_LOCK_ATIVO',
    'TFSTATE_MULTI_BUCKET', 'BUCKET_SEM_ACESSO', 'OBJECT_LOCK',
    'MFA_DELETE', 'ASSET_CATEGORY_INVALIDO',
}

REVIEW_TIPOS = {
    'TFSTATE_LEGADO', 'TFSTATE_BUCKET_ERRADO', 'TFSTATE_MULTI_BUCKET_NOVO_PATH',
    'REPLICATION_CROSS_ACCOUNT', 'SNS_NOTIFICATION', 'KMS_CMK_CUSTOMIZADA',
    'WEBSITE_HOSTING', 'ACL_CUSTOMIZADA', 'BUCKET_REGIAO_DIFERENTE',
    'LOGICAL_NAME_FORA_PADRAO', 'LOGGING_TARGET_INEXISTENTE',
}

SCRIPT_FLAGS = {
    'LC_CUSTOM': 'lifecycle_rules no main.tf (LC ≠ padrão BP)',
    'LC_DRIFT': 'IDs BP mas transição Glacier pode sumir — BP 2.2.1 ou lifecycle_rules',
    'LC_BP_NEW': 'sem LC na AWS — BP criará no apply (+create)',
    'SQS_KMS': 'fila com KMSAllows — precisa kms_keys + BP ≥2.2.1',
    'SQS_DEAD': 'ARN de fila inexistente no main.tf gerado',
    'REPLICATION': 'replication ativa — revisar replication_rules',
    'AES256': 'manter sse_algorithm AES256 no main.tf',
    'NOT_SUPPORTED': 'config BP não gerencia (inventory, website, …)',
    'SNS_UNTESTED': 'SNS notification — validar plan sem destroy',
    'LAMBDA_NOTIF': '+lambda_permission esperado no import',
}


def load_csv(path: Path, team: str | None, env: str | None, all_rows: bool) -> list[dict]:
    for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
        try:
            raw = path.read_text(encoding=enc).lstrip('﻿')
            sep = ';' if raw.split('\n')[0].count(';') > raw.split('\n')[0].count(',') else ','
            rows = [
                {k.strip(): (v.strip() if v else '') for k, v in r.items() if k}
                for r in csv.DictReader(raw.splitlines(), delimiter=sep)
            ]
            break
        except Exception:
            rows = None
    if not rows:
        raise SystemExit(f'Erro ao ler CSV: {path}')

    out = []
    for r in rows:
        bucket = r.get('bucket_name', '').strip()
        t = r.get('team', '').strip()
        e = r.get('env', '').strip()
        if not bucket or t in ('nan', 'None', ''):
            continue
        if not all_rows:
            if team and t != team:
                continue
            if env and e != env:
                continue
        out.append(r)
    return out


def check_sqs_queues(cfg: dict) -> list[dict]:
    """KMS na policy e filas inexistentes."""
    findings = []
    notif = cfg.get('notifications') or {}
    cached = cfg.get('sqs_queue_policies') or {}

    for q in notif.get('QueueConfigurations', []):
        arn = q.get('QueueArn', '')
        qname = arn.split(':')[-1] if arn else '?'
        if not arn:
            continue

        meta = cached.get(arn) or {}
        kms = meta.get('kms_keys') or []
        if not kms:
            doc, err = fetch_sqs_queue_policy_document(arn, return_error=True)
            if doc:
                kms = kms_keys_from_queue_policy_document(doc)
            elif err and 'NonExistentQueue' in str(err):
                findings.append({
                    'tipo': 'SQS_QUEUE_INEXISTENTE',
                    'sev': 'ALTO',
                    'msg': f'Fila {qname} não existe — notification órfã',
                    'fix': 'Corrigir sqs_notifications no regen ou remover notification na AWS',
                })
                continue
            elif err:
                findings.append({
                    'tipo': 'SQS_POLICY_ERRO',
                    'sev': 'MEDIO',
                    'msg': f'Não leu policy de {qname}: {err}',
                    'fix': 'Verificar permissão SQS ou ARN',
                })
                continue

        if kms:
            findings.append({
                'tipo': 'SQS_KMS_POLICY',
                'sev': 'ALTO',
                'msg': f'Fila {qname}: {len(kms)} CMK(s) em KMSAllows',
                'fix': 'Gerador deve emitir kms_keys; BP ref ≥2.2.1',
            })
    return findings


def analyze_lifecycle(cfg: dict, asset: str, env: str) -> tuple[str, list[str], list[dict]]:
    """Retorna (lc_mode, script_flags, issues)."""
    flags = []
    issues = []
    lc = cfg.get('lifecycle') or {}
    rules = lc.get('Rules', [])
    vers = (cfg.get('versioning') or {}).get('Status', '')

    if not rules:
        return 'bp_new', ['LC_BP_NEW'], issues

    if lifecycle_matches_bp_default(rules, asset, env, vers):
        drift = lifecycle_transition_drift_warnings(rules, asset, env)
        if drift:
            flags.append('LC_DRIFT')
            for w in drift:
                issues.append({
                    'tipo': 'LIFECYCLE_DRIFT_BP',
                    'sev': 'ALTO',
                    'msg': w,
                    'fix': 'BP 2.2.1 ou preservar lifecycle_rules no main.tf (decisão do time)',
                })
            return 'bp_match_drift', flags, issues
        return 'bp_match', flags, issues

    # Lifecycle não bate com padrão BP — custom
    flags.append('LC_CUSTOM')
    issues.append({
        'tipo': 'LIFECYCLE_CUSTOM',
        'sev': 'MEDIO',
        'msg': f'{len(rules)} regra(s) fora do subconjunto padrão BP',
        'fix': 'main.tf com lifecycle_rules — SRE do time valida antes do merge',
    })
    return 'custom', flags, issues


def analyze_extracted(cfg: dict, asset: str, env: str) -> tuple[list[str], list[dict]]:
    flags = []
    issues = []

    enc_rules = (cfg.get('encryption') or {}).get('ServerSideEncryptionConfiguration', {}).get('Rules', [])
    algo = ''
    if enc_rules:
        algo = enc_rules[0].get('ApplyServerSideEncryptionByDefault', {}).get('SSEAlgorithm', '')
    if algo == 'AES256':
        flags.append('AES256')
        issues.append({
            'tipo': 'AES256_BUCKET',
            'sev': 'ALTO',
            'msg': 'Bucket com SSE-S3 (AES256) — plan não pode trocar para aws:kms',
            'fix': 'sse_algorithm = AES256 explícito no main.tf gerado',
        })

    if get_kms_key_id(cfg):
        flags.append('KMS_CMK')  # informativo; preflight também cobre

    rep = (cfg.get('replication') or {}).get('ReplicationConfiguration', {})
    if rep.get('Rules'):
        flags.append('REPLICATION')
        issues.append({
            'tipo': 'REPLICATION_ATIVA',
            'sev': 'ALTO',
            'msg': f"{len(rep['Rules'])} regra(s) de replicação",
            'fix': 'Passo generate separado; revisar CHANGES e plan',
        })

    n = cfg.get('notifications') or {}
    if n.get('TopicConfigurations'):
        flags.append('SNS_UNTESTED')
    if n.get('LambdaFunctionConfigurations'):
        flags.append('LAMBDA_NOTIF')

    for ns_key, ns_msg in get_not_supported(cfg):
        flags.append('NOT_SUPPORTED')
        sev = 'CRITICO' if 'CRÍTICO' in ns_msg else 'MEDIO'
        issues.append({
            'tipo': f'NAO_SUPORTADO_{ns_key}',
            'sev': sev,
            'msg': ns_msg,
            'fix': 'ignore_changes ou exclusão manual — BP não gerencia',
        })

    issues.extend(check_sqs_queues(cfg))
    for i in issues:
        if i['tipo'] == 'SQS_KMS_POLICY' and 'SQS_KMS' not in flags:
            flags.append('SQS_KMS')
        if i['tipo'] == 'SQS_QUEUE_INEXISTENTE' and 'SQS_DEAD' not in flags:
            flags.append('SQS_DEAD')

    return flags, issues


def assign_tier(preflight_issues: list, script_flags: list, csv_blockers: str) -> str:
    if csv_blockers and csv_blockers.strip():
        return 'BLOCK'
    for i in preflight_issues:
        if i.get('tipo') in BLOCK_TIPOS or i.get('sev') == 'CRITICO':
            return 'BLOCK'
    for i in preflight_issues:
        if i.get('tipo') in REVIEW_TIPOS or i.get('sev') == 'ALTO':
            return 'REVIEW'
    hard_flags = {'LC_DRIFT', 'LC_CUSTOM', 'SQS_KMS', 'SQS_DEAD', 'REPLICATION',
                  'AES256', 'SNS_UNTESTED', 'NOT_SUPPORTED'}
    if hard_flags.intersection(script_flags):
        return 'REVIEW'
    for i in preflight_issues:
        if i.get('sev') in ('MEDIO',):
            return 'REVIEW'
    return 'AUTO'


def discover_one(row: dict, all_rows: list, configs_dir: Path, do_extract: bool, force_extract: bool, max_age_hours: int | None = None) -> dict:
    bucket = row.get('bucket_name', '').strip()
    team = row.get('team', '').strip()
    env = row.get('env', '').strip()
    asset = row.get('asset_category', '').strip()
    cat = row.get('category', '').strip()
    blockers = row.get('blockers', '').strip()

    _, _, _, _, pf_issues = scan_bucket(row, all_rows)

    cfg = None
    extract_status = 'SKIP'
    if do_extract:
        status, cfg = extract_bucket(bucket, configs_dir, force=force_extract,
                                     max_age_hours=max_age_hours)
        extract_status = status
    else:
        cfg = load_config(bucket, configs_dir)
        if cfg:
            extract_status = 'CACHED'

    script_flags = []
    script_issues = []
    lc_mode = 'unknown'

    if cfg:
        lc_mode, lc_flags, lc_issues = analyze_lifecycle(cfg, asset, env)
        script_flags.extend(lc_flags)
        script_issues.extend(lc_issues)
        ex_flags, ex_issues = analyze_extracted(cfg, asset, env)
        script_flags.extend(ex_flags)
        script_issues.extend(ex_issues)
        script_flags = sorted(set(script_flags))
    elif do_extract:
        script_flags.append('EXTRACT_FAILED')

    all_issues = pf_issues + script_issues
    tier = assign_tier(pf_issues, script_flags, blockers)

    script_ready = tier == 'AUTO' and extract_status in ('CACHED', 'EXTRACTED', 'SKIP')
    wave_hint = 'onda_auto' if tier == 'AUTO' else ('manual' if tier == 'BLOCK' else 'onda_review')

    return {
        'bucket': bucket,
        'team': team,
        'env': env,
        'category': cat,
        'asset_category': asset,
        'tier': tier,
        'lc_mode': lc_mode,
        'script_flags': script_flags,
        'script_flag_labels': [SCRIPT_FLAGS.get(f, f) for f in script_flags],
        'csv_blockers': blockers,
        'extract_status': extract_status,
        'has_notifications': bool(cfg and has_notifications(cfg)),
        'script_ready': script_ready,
        'wave_hint': wave_hint,
        'issue_count': len(all_issues),
        'issues': all_issues,
        'top_issues': '; '.join(
            f"{i.get('tipo','?')}" for i in sorted(
                all_issues,
                key=lambda x: SEV_ORDER.get(x.get('sev', 'INFO'), 9),
            )[:5]
        ),
    }


def write_csv(path: Path, records: list[dict]) -> None:
    fields = [
        'bucket', 'team', 'env', 'category', 'asset_category', 'tier',
        'lc_mode', 'script_flags', 'csv_blockers', 'extract_status',
        'script_ready', 'wave_hint', 'issue_count', 'top_issues',
    ]
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in records:
            row = dict(r)
            row['script_flags'] = '|'.join(r.get('script_flags', []))
            w.writerow(row)


def write_summary_md(path: Path, records: list[dict], env_label: str) -> None:
    by_tier = defaultdict(list)
    by_flag = defaultdict(int)
    for r in records:
        by_tier[r['tier']].append(r)
        for f in r.get('script_flags', []):
            by_flag[f] += 1

    lines = [
        f'# Discovery S3 — {env_label}',
        '',
        f'**Total:** {len(records)} buckets',
        f'| Tier | Qtd | Uso na onda |',
        f'|------|-----|-------------|',
    ]
    for tier in ('BLOCK', 'REVIEW', 'AUTO'):
        n = len(by_tier[tier])
        hint = {
            'BLOCK': 'Não incluir na onda até resolver CSV/blockers/tfstate',
            'REVIEW': 'Migrar com validate manual + CHANGES; pode ir em lote menor',
            'AUTO': 'Candidato a `./ondaX-{env}.sh` quase automático (BP 2.2.1)',
        }[tier]
        lines.append(f'| **{tier}** | {n} | {hint} |')

    lines += ['', '## Flags do script (agregado)', '']
    for flag, count in sorted(by_flag.items(), key=lambda x: -x[1]):
        lines.append(f'- `{flag}` ({count}): {SCRIPT_FLAGS.get(flag, flag)}')

    for tier in ('BLOCK', 'REVIEW'):
        if not by_tier[tier]:
            continue
        lines += ['', f'## {tier} — buckets', '']
        for r in sorted(by_tier[tier], key=lambda x: (x['team'], x['bucket']))[:80]:
            flags = ','.join(r.get('script_flags', [])[:4]) or '-'
            lines.append(
                f"- `{r['bucket']}` ({r['team']}/{r['env']}) "
                f"lc={r['lc_mode']} flags={flags} blockers={r['csv_blockers'] or '-'}"
            )
        if len(by_tier[tier]) > 80:
            lines.append(f'- ... +{len(by_tier[tier]) - 80} (ver CSV)')

    lines += [
        '',
        '## Fluxo recomendado HML/PRD',
        '',
        '1. `python3 s3_discovery.py --csv levantamento_completo.csv --env hml --extract --parallel 8`',
        '2. Revisar `discovery_report.csv` — filtrar `tier=BLOCK`',
        '3. Corrigir blockers / atualizar CSV',
        '4. Onda só com `tier=AUTO` ou `REVIEW` com validate-one nos canários',
        '5. `BP_SOURCE` ref **2.2.1** antes do generate',
        '',
        '## O que o script faz sozinho vs time',
        '',
        '| Script | Time dono |',
        '|--------|-------------|',
        '| import limpo, BP pin, HCL, kms_keys, AES256, lifecycle_rules se custom | Aceitar +create LC ou manter regras custom |',
        '| preflight tfstate | Locks, states multi-bucket |',
        '',
    ]
    path.write_text('\n'.join(lines), encoding='utf-8')


def print_console(records: list[dict], tier_filter: str | None) -> None:
    shown = [r for r in records if not tier_filter or r['tier'] == tier_filter]
    by_tier = defaultdict(int)
    for r in records:
        by_tier[r['tier']] += 1

    print(f"\n{'=' * 65}")
    print(' S3 DISCOVERY — resultado')
    print(f"{'=' * 65}")
    for tier in ('BLOCK', 'REVIEW', 'AUTO'):
        icon = {'BLOCK': '🔴', 'REVIEW': '🟠', 'AUTO': '🟢'}[tier]
        print(f" {icon} {tier}: {by_tier[tier]}")
    print(f"{'=' * 65}\n")

    for r in sorted(shown, key=lambda x: (TIER_ORDER.get(x['tier'], 9), x['team'], x['bucket'])):
        icon = {'BLOCK': '🔴', 'REVIEW': '🟠', 'AUTO': '🟢'}[r['tier']]
        flags = ' '.join(r.get('script_flags', [])[:6]) or '-'
        print(f" {icon} {r['bucket']} [{r['team']}/{r['env']}] lc={r['lc_mode']} {flags}")


def main() -> int:
    ap = argparse.ArgumentParser(description='Discovery S3 unificado (preflight + extract + script)')
    ap.add_argument('--csv', type=Path, default=SCRIPT_DIR / 'levantamento_completo.csv')
    ap.add_argument('--team', default=None)
    ap.add_argument('--env', default=None, help='dev | hml | prd')
    ap.add_argument('--all', action='store_true', help='Todos os envs (ignora --env)')
    ap.add_argument('--extract', action='store_true',
                    help='Extrair .s3config.json (senão usa cache em --configs-dir)')
    ap.add_argument('--force-extract', action='store_true')
    ap.add_argument('--max-cache-age', type=int, default=None, metavar='HORAS',
                    help='Re-extrai config se cache tiver mais de N horas')
    ap.add_argument('--configs-dir', type=Path, default=DEFAULT_CONFIGS)
    ap.add_argument('--out-dir', type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument('--parallel', type=int, default=6)
    ap.add_argument('--tier', choices=['BLOCK', 'REVIEW', 'AUTO'],
                    help='Filtrar saída no console')
    ap.add_argument('--output-prefix', default='discovery_report',
                    help='Prefixo dos arquivos em --out-dir')
    args = ap.parse_args()

    if not args.csv.exists():
        print(f'CSV não encontrado: {args.csv}', file=sys.stderr)
        return 1

    rows = load_csv(args.csv, args.team, args.env, args.all)
    if not rows:
        print('Nenhum bucket selecionado.', file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.extract:
        args.configs_dir.mkdir(parents=True, exist_ok=True)

    env_label = args.env or ('all' if args.all else 'filtered')
    print(f'\n Discovery: {len(rows)} bucket(s) | env={env_label} | '
          f'extract={"sim" if args.extract else "cache"} | workers={args.parallel}\n')

    records = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {
            ex.submit(
                discover_one, r, rows, args.configs_dir,
                args.extract, args.force_extract,
                getattr(args, 'max_cache_age', None),
            ): r
            for r in rows
        }
        done = 0
        for fut in as_completed(futs):
            done += 1
            rec = fut.result()
            records.append(rec)
            icon = {'BLOCK': '🔴', 'REVIEW': '🟠', 'AUTO': '🟢'}[rec['tier']]
            print(f" [{done:>4}/{len(rows)}] {icon} {rec['bucket']} ({rec['tier']})")

    records.sort(key=lambda x: (TIER_ORDER.get(x['tier'], 9), x['team'], x['env'], x['bucket']))

    prefix = args.output_prefix
    if args.env and not args.all:
        env_suffix = f'_{args.env}'
        if not prefix.endswith(env_suffix):
            prefix = f'{prefix}{env_suffix}'
    json_path = args.out_dir / f'{prefix}.json'
    csv_path = args.out_dir / f'{prefix}.csv'
    md_path = args.out_dir / f'{prefix}_summary.md'

    json_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    write_csv(csv_path, records)
    write_summary_md(md_path, records, env_label)
    print_console(records, args.tier)

    print(f'\n JSON: {json_path}')
    print(f' CSV: {csv_path}')
    print(f' MD: {md_path}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())

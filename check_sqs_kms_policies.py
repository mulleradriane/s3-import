#!/usr/bin/env python3
"""
Lista filas SQS ligadas a buckets migrados e indica se a queue policy tem statement KMS
(precisa kms_keys no main.tf após BP ≥2.2.1).

Fontes de filas (por prioridade):
1) *.s3config.json (--configs-dir, default mr_output/.s3configs)
2) main.tf com sqs_notifications (--scan-mr-output)

Uso:
cd script
python3 check_sqs_kms_policies.py
python3 check_sqs_kms_policies.py --configs-dir mr_output/.s3configs --csv out.csv
python3 check_sqs_kms_policies.py --bucket ecs-events-default-dev
python3 check_sqs_kms_policies.py --scan-mr-output # inclui paths main.tf + kms_keys no HCL
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from s3_config_extractor import (
    fetch_sqs_queue_policy_document,
    kms_keys_from_queue_policy_document,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIGS = SCRIPT_DIR / 'mr_output' / '.s3configs'
DEFAULT_MR_OUTPUT = SCRIPT_DIR / 'mr_output'

QUEUE_ARN_RE = re.compile(
    r'queue_arn\s*=\s*"(arn:aws:sqs:[^"]+)"',
    re.MULTILINE,
)
KMS_KEYS_IN_HCL_RE = re.compile(r'kms_keys\s*=')


def queues_from_s3config(cfg_path: Path) -> list[dict]:
    cfg = json.loads(cfg_path.read_text())
    bucket = cfg.get('_bucket') or cfg_path.stem.replace('.s3config', '')
    rows = []
    for q in (cfg.get('notifications') or {}).get('QueueConfigurations', []):
        arn = q.get('QueueArn', '')
        if arn:
            rows.append({
                'bucket': bucket,
                'queue_name': arn.split(':')[-1],
                'queue_arn': arn,
                'notification_id': q.get('Id', ''),
                'source': str(cfg_path),
            })
    return rows


def queues_from_main_tf(tf_path: Path) -> list[dict]:
    text = tf_path.read_text(errors='ignore')
    if 'sqs_notifications' not in text:
        return []
    bucket = None
    for pat in (
        r'tag_legacy_name\s*=\s*"([^"]+)"',
        r'id\s*=\s*"(ecs-[^"]+)"',
    ):
        m = re.search(pat, text)
        if m:
            bucket = m.group(1)
            break
    if not bucket:
        # path: .../services/s3/<logical>/dev/main.tf — não é o nome do bucket
        bucket = tf_path.parts[-4] if len(tf_path.parts) >= 4 else tf_path.stem

    has_kms_hcl = bool(KMS_KEYS_IN_HCL_RE.search(text))
    rows = []
    for arn in QUEUE_ARN_RE.findall(text):
        rows.append({
            'bucket': bucket,
            'queue_name': arn.split(':')[-1],
            'queue_arn': arn,
            'notification_id': '',
            'source': str(tf_path),
            'kms_keys_in_hcl': has_kms_hcl,
        })
    return rows


def collect_queue_rows(
    configs_dir: Path | None,
    scan_mr_output: bool,
    mr_output: Path,
    bucket_filter: str | None,
) -> list[dict]:
    seen_arns: set[str] = set()
    rows: list[dict] = []

    def add(row: dict) -> None:
        arn = row['queue_arn']
        if arn in seen_arns:
            return
        if bucket_filter and bucket_filter not in row['bucket']:
            return
        seen_arns.add(arn)
        rows.append(row)

    if configs_dir and configs_dir.is_dir():
        for p in sorted(configs_dir.glob('*.s3config.json')):
            for row in queues_from_s3config(p):
                add(row)

    if scan_mr_output and mr_output.is_dir():
        for p in sorted(mr_output.rglob('main.tf')):
            if '.terraform' in p.parts:
                continue
            for row in queues_from_main_tf(p):
                add(row)

    return sorted(rows, key=lambda r: (r['bucket'], r['queue_name']))


def inspect_queue(row: dict) -> dict:
    arn = row['queue_arn']
    doc, err = fetch_sqs_queue_policy_document(arn, return_error=True)
    if doc is None:
        return {
            **row,
            'policy_status': err or 'no_policy_or_error',
            'has_kms_statement': False,
            'kms_keys': [],
            'kms_count': 0,
        }
    kms = kms_keys_from_queue_policy_document(doc)
    return {
        **row,
        'policy_status': 'ok',
        'has_kms_statement': bool(kms),
        'kms_keys': kms,
        'kms_count': len(kms),
    }


def print_table(results: list[dict]) -> None:
    needs = [r for r in results if r['has_kms_statement']]
    no_kms = [r for r in results if not r['has_kms_statement'] and r['policy_status'] == 'ok']
    errors = [r for r in results if r['policy_status'] != 'ok']

    print(f'\n{"=" * 72}')
    print(f'Filas SQS: {len(results)} | COM KMS (precisa kms_keys): {len(needs)} | '
          f'sem KMS: {len(no_kms)} | sem policy/erro: {len(errors)}')
    print(f'{"=" * 72}\n')

    if needs:
        print('## COM statement KMS na queue policy → regen com kms_keys + BP ≥2.2.1\n')
        print(f'{"Bucket":<42} {"Fila":<38} {"#CMK":>4} HCL kms_keys')
        print('-' * 100)
        for r in needs:
            hcl = 'sim' if r.get('kms_keys_in_hcl') else 'NÃO'
            print(f'{r["bucket"]:<42} {r["queue_name"]:<38} {r["kms_count"]:>4} {hcl}')
            for k in r['kms_keys']:
                print(f'  {k}')
        print()

    if no_kms:
        print('## SQS notification — policy SEM KMS (só SendMessage ou equivalente)\n')
        for r in no_kms:
            print(f'  {r["bucket"]} → {r["queue_name"]}')
        print()

    if errors:
        print('## Sem policy legível ou erro AWS CLI\n')
        err_samples: dict[str, int] = {}
        for r in errors:
            err_samples[r['policy_status']] = err_samples.get(r['policy_status'], 0) + 1
            print(f'  {r["bucket"]} → {r["queue_name"]} ({r["policy_status"]})')
        print()
        if len({r['policy_status'] for r in errors}) == 1 and 'AccessDenied' not in str(err_samples):
            e = next(iter(err_samples))
            print(f'  Dica: erro uniforme "{e}" — rode na sua máquina com `aws sts get-caller-identity` OK.\n')


def write_csv(path: Path, results: list[dict]) -> None:
    fields = [
        'bucket', 'queue_name', 'queue_arn', 'has_kms_statement', 'kms_count',
        'kms_keys_in_hcl', 'policy_status', 'kms_keys', 'source',
    ]
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in results:
            row = dict(r)
            row['kms_keys'] = '|'.join(r.get('kms_keys') or [])
            row['kms_keys_in_hcl'] = r.get('kms_keys_in_hcl', False)
            w.writerow(row)
    print(f'CSV: {path}')


def main() -> int:
    ap = argparse.ArgumentParser(description='Audita KMS em policies SQS das notifications migradas')
    ap.add_argument('--configs-dir', type=Path, default=DEFAULT_CONFIGS,
                    help='Diretório de *.s3config.json')
    ap.add_argument('--no-configs', action='store_true', help='Não ler s3config')
    ap.add_argument('--scan-mr-output', action='store_true',
                    help='Também varrer main.tf em mr_output')
    ap.add_argument('--mr-output', type=Path, default=DEFAULT_MR_OUTPUT)
    ap.add_argument('--bucket', help='Filtrar por substring no nome do bucket')
    ap.add_argument('--csv', type=Path, help='Exportar CSV')
    ap.add_argument('--json', type=Path, help='Exportar JSON completo')
    args = ap.parse_args()

    configs_dir = None if args.no_configs else args.configs_dir
    scan_mr = args.scan_mr_output

    rows = collect_queue_rows(
        configs_dir=configs_dir,
        scan_mr_output=scan_mr,
        mr_output=args.mr_output,
        bucket_filter=args.bucket,
    )
    if not rows:
        print('Nenhuma fila SQS encontrada. Use --configs-dir ou --scan-mr-output.', file=sys.stderr)
        return 1

    print(f'Consultando {len(rows)} fila(s) na AWS...')
    results = [inspect_queue(r) for r in rows]

    print_table(results)

    if args.csv:
        write_csv(args.csv, results)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f'JSON: {args.json}')

    return 0 if results else 1


if __name__ == '__main__':
    sys.exit(main())

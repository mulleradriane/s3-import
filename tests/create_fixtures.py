#!/usr/bin/env python3
"""
Cria os buckets de teste na AWS com todas as configurações dos cenários.

Uso:
  python3 tests/create_fixtures.py --prefix s3mig-test --region us-east-1
  python3 tests/create_fixtures.py --prefix s3mig-test --scenario plain aes256

Pré-requisitos:
  - AWS credentials configuradas (aws sso login ou variáveis de ambiente)
  - Permissões: s3:CreateBucket + s3:Put* para configs de bucket
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
from scenarios import SCENARIOS


def bucket_name(prefix: str, scenario_id: str, scenario: dict) -> str:
    """Gera o nome do bucket de teste para um cenário."""
    if scenario.get('legacy_name'):
        return f"s3-{prefix}-{scenario_id}"
    return f"{prefix}-{scenario_id}"


def create_bucket(s3, name: str, region: str) -> bool:
    """Cria o bucket na região especificada."""
    try:
        if region == 'us-east-1':
            s3.create_bucket(Bucket=name)
        else:
            s3.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={'LocationConstraint': region}
            )
        print(f"  ✅ Criado: {name}")
        return True
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('BucketAlreadyOwnedByYou', 'BucketAlreadyExists'):
            print(f"  ⏭  Já existe: {name}")
            return True
        print(f"  ❌ Erro ao criar {name}: {e}", file=sys.stderr)
        return False


def tag_bucket(s3, name: str, scenario: dict, prefix: str) -> None:
    """Aplica tags padrão ECS ao bucket de teste."""
    env = scenario.get('env', 'dev')
    asset_cat = scenario.get('asset_cat', 'Productive data')
    team = scenario.get('team', 's3mig')

    s3.put_bucket_tagging(
        Bucket=name,
        Tagging={'TagSet': [
            {'Key': 'Team',           'Value': team},
            {'Key': 'Product',        'Value': team},
            {'Key': 'Application',    'Value': scenario['id']},
            {'Key': 'Environment',    'Value': env},
            {'Key': 'Asset_Category', 'Value': asset_cat},
            {'Key': 'Ticket',         'Value': 'S3MIG-TEST'},
            {'Key': 'AppId',          'Value': 'test'},
            {'Key': 'BusinessServices', 'Value': 'test'},
            {'Key': 'CostString',     'Value': 'test'},
            {'Key': 'DataType',       'Value': 'test'},
            {'Key': 'DataCategory',   'Value': 'test'},
            {'Key': 'Repository',     'Value': 's3mig-tests'},
            {'Key': 's3mig_test_prefix', 'Value': prefix},  # para cleanup
        ]}
    )


def apply_setup(s3, name: str, scenario: dict, region: str) -> bool:
    """Aplica a configuração específica do cenário."""
    setup_fn = scenario.get('setup')
    if not setup_fn:
        return True
    try:
        setup_fn(s3, name)
        return True
    except ClientError as e:
        code = e.response['Error']['Code']
        # Alguns erros são esperados (ex: object lock só pode ser ativado na criação)
        print(f"  ⚠️  Config parcial ({code}): {e.response['Error']['Message']}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ⚠️  Erro na setup: {e}", file=sys.stderr)
        return False


def write_csv(buckets: list[dict], output_path: Path) -> None:
    """Gera o CSV de levantamento para os buckets de teste."""
    import csv
    fields = ['bucket_name', 'team', 'env', 'asset_category', 'category', 'blockers']
    with output_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for b in buckets:
            w.writerow({
                'bucket_name':   b['name'],
                'team':          b['team'],
                'env':           b['env'],
                'asset_category': b['asset_cat'],
                'category':      'A',
                'blockers':      '',
            })
    print(f"\n  CSV gerado: {output_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description='Cria buckets de teste na AWS')
    ap.add_argument('--prefix',   required=True, help='Prefixo dos buckets (ex: s3mig-test)')
    ap.add_argument('--region',   default='us-east-1')
    ap.add_argument('--scenario', nargs='+', help='Filtrar por scenario_id (padrão: todos)')
    ap.add_argument('--csv-out',  default='tests/test_levantamento.csv',
                    help='Onde salvar o CSV de levantamento')
    ap.add_argument('--dry-run',  action='store_true', help='Só mostra o que faria')
    args = ap.parse_args()

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if s['id'] in args.scenario]

    if not scenarios:
        print("Nenhum cenário selecionado.", file=sys.stderr)
        return 1

    s3 = boto3.client('s3', region_name=args.region)

    print(f"\n  Criando {len(scenarios)} bucket(s) com prefixo '{args.prefix}'...\n")

    created = []
    for sc in scenarios:
        name = bucket_name(args.prefix, sc['id'], sc)
        print(f"  [{sc['id']}] {sc['description']}")

        if args.dry_run:
            print(f"    (dry-run) Criaria: {name}")
            created.append({'name': name, 'scenario': sc['id'], **sc})
            continue

        ok = create_bucket(s3, name, args.region)
        if not ok:
            continue

        time.sleep(0.5)  # evita rate limit
        tag_bucket(s3, name, sc, args.prefix)
        apply_setup(s3, name, sc, args.region)
        created.append({'name': name, 'scenario': sc['id'], **sc})
        print()

    if created:
        write_csv(created, Path(args.csv_out))

    print(f"\n  {len(created)} bucket(s) {'listados' if args.dry_run else 'criados'}.")
    print(f"  Próximo passo:")
    print(f"    python3 tests/run_tests.py --prefix {args.prefix} --csv {args.csv_out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

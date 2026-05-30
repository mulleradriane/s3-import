#!/usr/bin/env python3
"""
Remove todos os buckets de teste criados por create_fixtures.py.

Uso:
  python3 tests/cleanup.py --prefix s3mig-test
  python3 tests/cleanup.py --prefix s3mig-test --dry-run
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError


def empty_bucket(s3, name: str) -> None:
    """Esvazia o bucket antes de deletar (versioned + não-versioned)."""
    # Objetos normais
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=name):
        objects = [{'Key': o['Key']} for o in page.get('Contents', [])]
        if objects:
            s3.delete_objects(Bucket=name, Delete={'Objects': objects})

    # Versões e delete markers
    paginator = s3.get_paginator('list_object_versions')
    for page in paginator.paginate(Bucket=name):
        versions = [
            {'Key': v['Key'], 'VersionId': v['VersionId']}
            for v in page.get('Versions', [])
        ]
        markers = [
            {'Key': m['Key'], 'VersionId': m['VersionId']}
            for m in page.get('DeleteMarkers', [])
        ]
        to_delete = versions + markers
        if to_delete:
            s3.delete_objects(Bucket=name, Delete={'Objects': to_delete})


def delete_bucket(s3, name: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  (dry-run) Deletaria: {name}")
        return True
    try:
        empty_bucket(s3, name)
        s3.delete_bucket(Bucket=name)
        print(f"  ✅ Deletado: {name}")
        return True
    except ClientError as e:
        code = e.response['Error']['Code']
        if code == 'NoSuchBucket':
            print(f"  ⏭  Já não existe: {name}")
            return True
        print(f"  ❌ Erro ao deletar {name}: {e}", file=sys.stderr)
        return False


def find_test_buckets(s3, prefix: str) -> list[str]:
    """Lista todos os buckets que pertencem ao prefixo de teste."""
    response = s3.list_buckets()
    all_buckets = [b['Name'] for b in response.get('Buckets', [])]

    test_buckets = []
    for name in all_buckets:
        if not name.startswith(prefix):
            continue
        # Confirma via tag
        try:
            tags = s3.get_bucket_tagging(Bucket=name)
            tag_map = {t['Key']: t['Value'] for t in tags.get('TagSet', [])}
            if tag_map.get('s3mig_test_prefix') == prefix:
                test_buckets.append(name)
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchTagSet':
                # Bucket do prefixo mas sem tags — inclui por segurança
                if name.startswith(prefix + '-') or name.startswith('s3-' + prefix):
                    test_buckets.append(name)

    return sorted(test_buckets)


def main() -> int:
    ap = argparse.ArgumentParser(description='Remove buckets de teste do S3')
    ap.add_argument('--prefix',   required=True, help='Prefixo usado em create_fixtures.py')
    ap.add_argument('--region',   default='us-east-1')
    ap.add_argument('--dry-run',  action='store_true')
    ap.add_argument('--yes',      action='store_true', help='Pula confirmação')
    args = ap.parse_args()

    s3 = boto3.client('s3', region_name=args.region)

    buckets = find_test_buckets(s3, args.prefix)
    if not buckets:
        print(f"Nenhum bucket encontrado com prefixo '{args.prefix}'.")
        return 0

    print(f"\n  {len(buckets)} bucket(s) para deletar:")
    for name in buckets:
        print(f"    - {name}")

    if not args.dry_run and not args.yes:
        resp = input(f"\n  Deletar todos? [s/N] ").strip().lower()
        if resp not in ('s', 'sim', 'y', 'yes'):
            print("  Cancelado.")
            return 0

    print()
    failed = 0
    for name in buckets:
        if not delete_bucket(s3, name, args.dry_run):
            failed += 1

    print(f"\n  {len(buckets) - failed} deletados, {failed} com erro.")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

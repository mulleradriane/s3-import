"""
Cenários de teste para o pipeline S3 Migration.

Cada cenário define:
  id          — identificador curto (usado no nome do bucket)
  description — descrição humana
  env         — dev | hml | prd
  asset_cat   — categoria de asset (para lifecycle e versioning)
  team        — time (padrão: s3mig)
  setup       — função que recebe (boto3_s3, bucket_name) e configura o bucket
  expects     — dicionário com validações esperadas no output
  tier        — tier esperado no discovery (AUTO | REVIEW | BLOCK)
"""

from __future__ import annotations
import json


def _policy_allow_read(bucket_name: str) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowTestRead",
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
            "Condition": {"StringEquals": {"aws:PrincipalOrgID": "o-test"}}
        }]
    })


# ─── Setup functions (chamadas com s3_client, bucket_name) ────────────────────

def _setup_plain(s3, name):
    """Apenas cria o bucket — sem configs extras."""
    pass


def _setup_aes256(s3, name):
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]
        }
    )


def _setup_lc_productive(s3, name):
    """Lifecycle que bate com o padrão BP para Productive data."""
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={'Rules': [
            {
                'ID': '90 StandardIA -> 180 Glacier',
                'Status': 'Enabled',
                'Filter': {'Prefix': ''},
                'Transitions': [
                    {'Days': 90,  'StorageClass': 'STANDARD_IA'},
                    {'Days': 180, 'StorageClass': 'GLACIER_IR'},
                ],
            }
        ]}
    )


def _setup_lc_custom(s3, name):
    """Lifecycle custom — não bate com nenhum padrão BP."""
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={'Rules': [
            {
                'ID': 'custom-rule',
                'Status': 'Enabled',
                'Filter': {'Prefix': 'data/'},
                'Transitions': [
                    {'Days': 45,  'StorageClass': 'STANDARD_IA'},
                    {'Days': 120, 'StorageClass': 'GLACIER'},
                ],
                'Expiration': {'Days': 365},
            }
        ]}
    )


def _setup_lc_logs(s3, name):
    """Lifecycle para Logs/Backup: 30d→Glacier IR, 180d→Glacier."""
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={'Rules': [
            {
                'ID': '30 Glacier',
                'Status': 'Enabled',
                'Filter': {'Prefix': ''},
                'Transitions': [
                    {'Days': 30,  'StorageClass': 'GLACIER_IR'},
                    {'Days': 180, 'StorageClass': 'GLACIER'},
                ],
            }
        ]}
    )


def _setup_lc_cache(s3, name):
    """Lifecycle para Cache: expira em 45 dias."""
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={'Rules': [
            {
                'ID': 'Expira em 45 dias',
                'Status': 'Enabled',
                'Filter': {'Prefix': ''},
                'Expiration': {'Days': 45},
            }
        ]}
    )


def _setup_lc_dev(s3, name):
    """Lifecycle para Development: 30d→StandardIA, 90d→Glacier IR."""
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={'Rules': [
            {
                'ID': '30 StandardIA -> 90 Glacier',
                'Status': 'Enabled',
                'Filter': {'Prefix': ''},
                'Transitions': [
                    {'Days': 30, 'StorageClass': 'STANDARD_IA'},
                    {'Days': 90, 'StorageClass': 'GLACIER_IR'},
                ],
            }
        ]}
    )


def _setup_versioning(s3, name):
    s3.put_bucket_versioning(
        Bucket=name,
        VersioningConfiguration={'Status': 'Enabled'}
    )


def _setup_policy(s3, name):
    # Bloco público necessário desativado para aceitar policy
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            'BlockPublicAcls': False, 'IgnorePublicAcls': False,
            'BlockPublicPolicy': False, 'RestrictPublicBuckets': False,
        }
    )
    s3.put_bucket_policy(Bucket=name, Policy=_policy_allow_read(name))


def _setup_cors(s3, name):
    s3.put_bucket_cors(
        Bucket=name,
        CORSConfiguration={'CORSRules': [{
            'AllowedOrigins': ['https://app.example.com'],
            'AllowedMethods': ['GET', 'PUT'],
            'AllowedHeaders': ['*'],
            'MaxAgeSeconds': 3600,
        }]}
    )


def _setup_logging_custom(s3, name):
    """Logging para um bucket diferente do padrão BP."""
    # Nota: o bucket de destino deve existir — usamos o próprio bucket como destino
    # em testes para evitar criação de bucket adicional
    s3.put_bucket_logging(
        Bucket=name,
        BucketLoggingStatus={
            'LoggingEnabled': {
                'TargetBucket': name,
                'TargetPrefix': 'access-logs/',
            }
        }
    )


def _setup_website(s3, name):
    s3.put_bucket_website(
        Bucket=name,
        WebsiteConfiguration={
            'IndexDocument': {'Suffix': 'index.html'},
            'ErrorDocument': {'Key': 'error.html'},
        }
    )


def _setup_replication(s3, name, dest_bucket: str | None = None):
    """Replication — precisa de versioning e role IAM, configura o mínimo."""
    s3.put_bucket_versioning(
        Bucket=name,
        VersioningConfiguration={'Status': 'Enabled'}
    )
    # Apenas marca a intenção — plan vai REVIEW, mas config inválida sem role real
    # Para testes, deixamos vazio e documentamos no cenário


def _setup_ownership_preferred(s3, name):
    """Object ownership diferente do padrão BP (BucketOwnerEnforced)."""
    s3.put_bucket_ownership_controls(
        Bucket=name,
        OwnershipControls={'Rules': [{'ObjectOwnership': 'BucketOwnerPreferred'}]}
    )


def _setup_multi(s3, name):
    """Múltiplas configs combinadas."""
    _setup_aes256(s3, name)
    _setup_cors(s3, name)
    _setup_versioning(s3, name)
    _setup_lc_custom(s3, name)


# ─── Cenários ─────────────────────────────────────────────────────────────────

SCENARIOS: list[dict] = [
    {
        'id': 'plain',
        'description': 'Bucket sem configs extras — apenas defaults AWS',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_plain,
        # AWS aplica AES256 por padrão desde 2023 → AES256 flag → REVIEW
        'tier': 'REVIEW',
        'expects': {
            'no_lifecycle_rules_in_tf': True,
            'no_destroy_in_plan': True,
            'no_replace_in_plan': True,
        },
    },
    {
        'id': 'aes256',
        'description': 'Bucket com SSE-S3 (AES256) — BP não pode trocar para KMS',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_aes256,
        'tier': 'REVIEW',
        'expects': {
            'tf_contains': ['sse_algorithm = "AES256"'],
            'changes_md_contains': ['AES256'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'lc-match',
        'description': 'Lifecycle que bate com o padrão BP (Productive data, prd)',
        'env': 'prd', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_lc_productive,
        'tier': 'REVIEW',  # AES256 default + LC_DRIFT,
        'expects': {
            'no_lifecycle_rules_in_tf': True,
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'lc-custom',
        'description': 'Lifecycle customizado — deve gerar lifecycle_rules no main.tf',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_lc_custom,
        'tier': 'REVIEW',
        'expects': {
            'tf_contains': ['lifecycle_rules'],
            'changes_md_contains': ['lifecycle', 'custom'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'lc-none',
        'description': 'Bucket sem lifecycle — BP criará no apply',
        'env': 'dev', 'asset_cat': 'Development', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'REVIEW',  # AES256 default,
        'expects': {
            'no_lifecycle_rules_in_tf': True,
            'changes_md_contains': ['BP criará', 'apply'],
        },
    },
    {
        'id': 'lc-logs',
        'description': 'Lifecycle padrão para Logs (30d→Glacier IR, 90d→Glacier)',
        'env': 'dev', 'asset_cat': 'Logs', 'team': 's3mig',
        'setup': _setup_lc_logs,
        'tier': 'REVIEW',  # AES256 default,
        'expects': {
            'no_lifecycle_rules_in_tf': True,
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'lc-cache',
        'description': 'Lifecycle padrão para Cache (expira em 45 dias)',
        'env': 'dev', 'asset_cat': 'Cache', 'team': 's3mig',
        'setup': _setup_lc_cache,
        'tier': 'REVIEW',  # AES256 default,
        'expects': {
            'no_lifecycle_rules_in_tf': True,
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'lc-dev',
        'description': 'Lifecycle padrão para Development (30d→IA, 90d→Glacier IR)',
        'env': 'dev', 'asset_cat': 'Development', 'team': 's3mig',
        'setup': _setup_lc_dev,
        'tier': 'REVIEW',  # AES256 default + LC_DRIFT,
        'expects': {
            'no_lifecycle_rules_in_tf': True,
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'versioning',
        'description': 'Versioning habilitado (prd + Productive data → BP gerencia)',
        'env': 'prd', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_versioning,
        'tier': 'REVIEW',  # AES256 default,
        'expects': {
            'tf_contains': ['versioning_configuration = "Enabled"'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'policy',
        'description': 'Bucket com bucket policy — deve gerar policy_json',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_policy,
        'tier': 'REVIEW',
        'expects': {
            'tf_contains': ['policy_json'],
            'changes_md_contains': ['policy'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'cors',
        'description': 'CORS configurado — deve gerar cors_rules',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_cors,
        'tier': 'REVIEW',  # AES256 default,
        'expects': {
            'tf_contains': ['cors_rules'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'logging-custom',
        'description': 'Logging para bucket diferente do padrão BP',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_logging_custom,
        'tier': 'REVIEW',
        'expects': {
            'tf_contains': ['logging_target_bucket'],
            'changes_md_contains': ['logging', 'destino'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'website',
        'description': 'Static website — NOT_SUPPORTED pela BP',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_website,
        'tier': 'REVIEW',
        'expects': {
            'changes_md_contains': ['website_configuration', 'ignore_changes'],
        },
    },
    {
        'id': 'ownership',
        'description': 'ObjectOwnership = BucketOwnerPreferred (não-padrão)',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_ownership_preferred,
        'tier': 'REVIEW',  # AES256 default + ACL_CUSTOMIZADA,
        'expects': {
            'tf_contains': ['BucketOwnerPreferred'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'multi',
        'description': 'Múltiplas configs combinadas (AES256 + CORS + versioning + LC custom)',
        'env': 'hml', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_multi,
        'tier': 'REVIEW',
        'expects': {
            'tf_contains': ['sse_algorithm = "AES256"', 'cors_rules', 'lifecycle_rules'],
            'no_destroy_in_plan': True,
        },
    },
    {
        'id': 'legacy',
        'description': 'Bucket com nome legado (s3- prefix) — tag_legacy_name obrigatório',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'REVIEW',  # AES256 default + LOGICAL_NAME_FORA_PADRAO,
        'legacy_name': True,     # sinaliza para usar nome no formato s3-{prefix}
        'expects': {
            'tf_contains': ['tag_legacy_name'],
            'no_destroy_in_plan': True,
        },
    },
    # ─── Cenários BLOCK ───────────────────────────────────────────────────────────
    {
        'id': 'block-invalid-cat',
        'description': 'asset_category inválido → ASSET_CATEGORY_INVALIDO → tier BLOCK',
        'env': 'dev', 'asset_cat': 'CATEGORIA_INVALIDA', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'BLOCK',
        # Cenários BLOCK param no discovery — não geram main.tf nem plan
        'expects': {
            'state_detected_as_legacy': False,  # sem state legado neste caso
        },
    },
    {
        'id': 'block-csv-blocker',
        'description': 'Bucket com blockers no CSV → tier BLOCK independente da config',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'BLOCK',
        'csv_blockers': 'aguardando aprovação legal',  # injetado no row do discovery
        'expects': {},
    },
    # ─── Cenários de State Migration ─────────────────────────────────────────────
    {
        'id': 'state-import',
        'description': 'Import limpo → plan pós-import sem create/destroy do bucket principal',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'REVIEW',  # AES256 default,
        'run_import': True,      # roda _import_commands.sh após terraform init
        'expects': {
            'no_destroy_in_plan': True,
            'no_create_bucket_in_plan': True,   # aws_s3_bucket.main deve estar no state
        },
    },
    {
        'id': 'state-migrate',
        'description': 'State em backend antigo → init -migrate-state → plan preserva state',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'REVIEW',  # AES256 default,
        'run_import': True,
        'run_state_migrate': True,  # testa migração de backend local-antigo → local-novo
        'expects': {
            'no_destroy_in_plan': True,
            'no_create_bucket_in_plan': True,
        },
    },
    {
        'id': 'state-v012',
        'description': 'State sintético TF 0.12 → detectado como legacy, força import limpo',
        'env': 'dev', 'asset_cat': 'Productive data', 'team': 's3mig',
        'setup': _setup_plain,
        'tier': 'REVIEW',  # AES256 default,
        'seed_legacy_state': '0.12.31',   # cria state sintético TF 0.12 antes do init
        'expects': {
            'no_destroy_in_plan': True,
            'state_detected_as_legacy': True,
        },
    },
]
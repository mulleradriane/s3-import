#!/usr/bin/env python3
"""
s3_config_extractor.py
Extrai TODAS as configurações de um bucket S3 em paralelo.
Salva em {output_dir}/{bucket_name}.s3config.json
Importado pelo s3_migrate.py — pode ser usado standalone também.

Uso standalone:
python3 s3_config_extractor.py --bucket meu-bucket --output-dir ./configs
python3 s3_config_extractor.py --csv levantamento_completo.csv --output-dir ./configs --parallel 8
"""

import argparse, csv, json, os, shlex, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_KMS_SQS_ACTIONS = frozenset({
    'kms:decrypt', 'kms:encrypt', 'kms:generatedatakey',
})

_lock = threading.Lock()

def run_aws(cmd):
    """Roda um comando aws cli e retorna (ok, data_dict)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        # Sucesso com output JSON
        if result.returncode == 0 and result.stdout.strip():
            return True, json.loads(result.stdout)
        # Sucesso sem output — configuração simplesmente não existe no bucket
        if result.returncode == 0 and not result.stdout.strip():
            return True, {}
        # Falha — analisa o stderr
        stderr = result.stderr.strip()
        if any(x in stderr for x in [
            'NoSuchLifecycleConfiguration','NoSuchCORSConfiguration',
            'NoSuchBucketPolicy','NoSuchReplicationConfiguration',
            'NoSuchWebsiteConfiguration','ServerSideEncryptionConfigurationNotFoundError',
            'ObjectLockConfigurationNotFoundError','NoSuchPublicAccessBlockConfiguration',
            'NoSuchTagSet','NoSuchConfiguration',
            'ReplicationConfigurationNotFoundError',
            'OwnershipControlsNotFoundError',
            'NoSuchIntelligentTieringConfiguration',
            'NoSuchInventoryConfiguration',
            'NoSuchAnalyticsConfiguration',
            'NoSuchMetricsConfiguration',
        ]):
            return True, {}  # Não existe — OK, só vazio
        if 'AccessDenied' in stderr:
            return False, {'_error': 'AccessDenied'}
        if 'NoSuchBucket' in stderr:
            return False, {'_error': 'NoSuchBucket'}
        return False, {'_error': stderr[:200] if stderr else 'unknown error'}
    except subprocess.TimeoutExpired:
        return False, {'_error': 'Timeout'}
    except Exception as e:
        return False, {'_error': str(e)}

def extract_full_config(bucket_name):
    """
    Extrai TODAS as configurações de um bucket em paralelo.
    Retorna um dict completo com tudo que foi encontrado.
    """
    result = {
        '_bucket': bucket_name,
        '_extracted_at': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
        '_errors': {},
        # Campos de configuração
        'lifecycle': None,
        'versioning': None,
        'encryption': None,
        'bucket_policy': None,
        'cors': None,
        'notifications': None,
        'replication': None,
        'logging': None,
        'ownership_controls': None,
        'public_access_block':None,
        'tagging': None,
        'metrics': None,
        # Não suportados pela BP
        'object_lock': None,
        'website': None,
        'accelerate': None,
        'request_payment': None,
        'intelligent_tiering':None,
        'inventory': None,
        'analytics': None,
    }

    # Comandos a executar
    commands = {
        'lifecycle': f'aws s3api get-bucket-lifecycle-configuration --bucket {bucket_name} --output json',
        'versioning': f'aws s3api get-bucket-versioning --bucket {bucket_name} --output json',
        'encryption': f'aws s3api get-bucket-encryption --bucket {bucket_name} --output json',
        'bucket_policy': f'aws s3api get-bucket-policy --bucket {bucket_name} --output json',
        'cors': f'aws s3api get-bucket-cors --bucket {bucket_name} --output json',
        'notifications': f'aws s3api get-bucket-notification-configuration --bucket {bucket_name} --output json',
        'replication': f'aws s3api get-bucket-replication --bucket {bucket_name} --output json',
        'logging': f'aws s3api get-bucket-logging --bucket {bucket_name} --output json',
        'ownership_controls': f'aws s3api get-bucket-ownership-controls --bucket {bucket_name} --output json',
        'public_access_block': f'aws s3api get-public-access-block --bucket {bucket_name} --output json',
        'tagging': f'aws s3api get-bucket-tagging --bucket {bucket_name} --output json',
        'metrics': f'aws s3api list-bucket-metrics-configurations --bucket {bucket_name} --output json',
        'object_lock': f'aws s3api get-object-lock-configuration --bucket {bucket_name} --output json',
        'website': f'aws s3api get-bucket-website --bucket {bucket_name} --output json',
        'accelerate': f'aws s3api get-bucket-accelerate-configuration --bucket {bucket_name} --output json',
        'request_payment': f'aws s3api get-bucket-request-payment --bucket {bucket_name} --output json',
        'intelligent_tiering': f'aws s3api list-bucket-intelligent-tiering-configurations --bucket {bucket_name} --output json',
        'inventory': f'aws s3api list-bucket-inventory-configurations --bucket {bucket_name} --output json',
        'analytics': f'aws s3api list-bucket-analytics-configurations --bucket {bucket_name} --output json',
    }

    # Executa todos os comandos em paralelo (inner parallelism por bucket)
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(run_aws, cmd): key for key, cmd in commands.items()}
        for future in as_completed(futures):
            key = futures[future]
            ok, data = future.result()
            if ok and data:
                result[key] = data
            elif not ok:
                result['_errors'][key] = data.get('_error', 'unknown')

    # Policies das filas SQS (KMSAllows etc.) — BP notification precisa de kms_keys
    sqs_policies = enrich_sqs_queue_policies(result.get('notifications'))
    if sqs_policies:
        result['sqs_queue_policies'] = sqs_policies

    return result

def extract_bucket(bucket_name, output_dir, force=False, max_age_hours=None):
    """Extrai config de um bucket e salva em arquivo."""
    out_file = Path(output_dir) / f"{bucket_name}.s3config.json"

    # Skip se já extraído (idempotente)
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

    config = extract_full_config(bucket_name)
    out_file.write_text(json.dumps(config, indent=2, ensure_ascii=False))

    errors = config.get('_errors', {})
    status = 'EXTRACTED' if not errors else 'EXTRACTED_WITH_ERRORS'
    return status, config

def load_config(bucket_name, configs_dir):
    """Carrega config salva de um bucket. Retorna None se não encontrado."""
    cfg_file = Path(configs_dir) / f"{bucket_name}.s3config.json"
    if not cfg_file.exists():
        return None
    try:
        return json.loads(cfg_file.read_text())
    except:
        return None

# ── Análise do config extraído ─────────────────────────────────────────────────

def has_notifications(cfg):
    n = cfg.get('notifications') or {}
    return bool(
        n.get('QueueConfigurations') or
        n.get('TopicConfigurations') or
        n.get('LambdaFunctionConfigurations') or
        n.get('EventBridgeConfiguration')
    )

def get_kms_key_id(cfg):
    """Extrai KMS key ID customizado se existir."""
    enc = cfg.get('encryption') or {}
    rules = enc.get('ServerSideEncryptionConfiguration', {}).get('Rules', [])
    for rule in rules:
        sse = rule.get('ApplyServerSideEncryptionByDefault', {})
        if sse.get('SSEAlgorithm') == 'aws:kms':
            key_id = sse.get('KMSMasterKeyID', '')
            # Chave padrão (alias/aws/s3 ou vazia) não é CMK
            if key_id and 'alias/aws/s3' not in key_id and 'aws/s3' not in key_id:
                return key_id
    return None

def get_not_supported(cfg):
    """Retorna lista de configurações presentes que a BP não suporta."""
    not_sup = []
    if cfg.get('object_lock') and cfg['object_lock'].get('ObjectLockConfiguration'):
        not_sup.append(('object_lock', 'CRÍTICO — Object Lock não pode ser desativado após habilitado'))
    if cfg.get('website') and cfg['website'].get('IndexDocument'):
        not_sup.append(('website_configuration', 'MÉDIO — Static website hosting'))
    accel = (cfg.get('accelerate') or {}).get('Status','')
    if accel == 'Enabled':
        not_sup.append(('accelerate_configuration', 'BAIXO — Transfer acceleration ativado'))
    payment = (cfg.get('request_payment') or {}).get('Payer','')
    if payment == 'Requester':
        not_sup.append(('request_payment', 'BAIXO — Requester pays ativado'))
    tiering = cfg.get('intelligent_tiering') or {}
    if tiering.get('IntelligentTieringConfigurationList'):
        not_sup.append(('intelligent_tiering', 'MÉDIO — Intelligent Tiering configurado'))
    inv = cfg.get('inventory') or {}
    if inv.get('InventoryConfigurationList'):
        not_sup.append(('inventory', 'BAIXO — Inventory configurado'))
    analytics = cfg.get('analytics') or {}
    if analytics.get('AnalyticsConfigurationList'):
        not_sup.append(('analytics', 'BAIXO — Analytics configurado'))
    # Métricas customizadas (BP só suporta "Default")
    metrics_data = cfg.get('metrics') or {}
    custom_metrics = [m for m in metrics_data.get('MetricsConfigurationList', [])
                      if m.get('Id') != 'Default']
    if custom_metrics:
        ids = ', '.join(m.get('Id','?') for m in custom_metrics)
        not_sup.append(('custom_metrics', f'BAIXO — Métricas customizadas ({ids}) — BP só suporta "Default"'))
    return not_sup

def get_public_access_warnings(cfg):
    """Retorna avisos se o bucket tiver acesso público."""
    pab = (cfg.get('public_access_block') or {}).get('PublicAccessBlockConfiguration', {})
    warnings = []
    if not pab.get('BlockPublicAcls', True):
        warnings.append('BlockPublicAcls está DESATIVADO — BP vai ativar no apply')
    if not pab.get('BlockPublicPolicy', True):
        warnings.append('BlockPublicPolicy está DESATIVADO — BP vai ativar no apply')
    if not pab.get('IgnorePublicAcls', True):
        warnings.append('IgnorePublicAcls está DESATIVADO — BP vai ativar no apply')
    if not pab.get('RestrictPublicBuckets', True):
        warnings.append('RestrictPublicBuckets está DESATIVADO — BP vai ativar no apply')
    return warnings

def get_logging_info(cfg):
    """Retorna info de logging atual."""
    log = (cfg.get('logging') or {}).get('LoggingEnabled', {})
    if not log:
        return None, None
    return log.get('TargetBucket'), log.get('TargetPrefix')

def get_ownership(cfg):
    """Retorna ownership control atual."""
    oc = cfg.get('ownership_controls') or {}
    rules = oc.get('OwnershipControls', {}).get('Rules', [])
    if rules:
        return rules[0].get('ObjectOwnership', 'BucketOwnerEnforced')
    return 'BucketOwnerEnforced'


def sqs_arn_to_queue_url(queue_arn):
    """arn:aws:sqs:region:account:name → URL da API SQS."""
    parts = (queue_arn or '').split(':')
    if len(parts) < 6 or parts[2] != 'sqs':
        return None
    region, account, qname = parts[3], parts[4], parts[5]
    return f'https://sqs.{region}.amazonaws.com/{account}/{qname}'


def _normalize_iam_actions(action):
    if isinstance(action, str):
        return [action.lower()]
    if isinstance(action, list):
        return [a.lower() for a in action if isinstance(a, str)]
    return []


def _statement_principal_is_s3(st):
    """True se o statement permite o serviço S3 (ou Principal ausente/genérico)."""
    princ = st.get('Principal')
    if princ is None:
        return True
    if princ == '*':
        return True
    if isinstance(princ, str):
        return princ in ('*', 's3.amazonaws.com')
    if isinstance(princ, dict):
        svc = princ.get('Service')
        if svc is None:
            return True
        if isinstance(svc, str):
            return svc == 's3.amazonaws.com'
        if isinstance(svc, list):
            return 's3.amazonaws.com' in svc
    return False


def kms_keys_from_queue_policy_document(policy_doc):
    """
    Extrai ARNs de CMK de statements KMS na policy da fila (ex.: Sid KMSAllows para s3.amazonaws.com).
    """
    if isinstance(policy_doc, str):
        try:
            policy_doc = json.loads(policy_doc)
        except json.JSONDecodeError:
            return []
    if not isinstance(policy_doc, dict):
        return []

    statements = policy_doc.get('Statement', [])
    if isinstance(statements, dict):
        statements = [statements]

    keys = []
    for st in statements:
        if not isinstance(st, dict):
            continue
        if (st.get('Effect') or '').lower() != 'allow':
            continue
        if not _KMS_SQS_ACTIONS.intersection(_normalize_iam_actions(st.get('Action'))):
            continue
        if not _statement_principal_is_s3(st):
            continue
        resources = st.get('Resource', [])
        if isinstance(resources, str):
            resources = [resources]
        for r in resources or []:
            if isinstance(r, str) and ':kms:' in r:
                keys.append(r)
    return sorted(set(keys))


def fetch_sqs_queue_policy_document(queue_arn, return_error=False):
    """Lê Attributes.Policy da fila. Retorna dict ou None (ou tuple com erro se return_error)."""
    url = sqs_arn_to_queue_url(queue_arn)
    if not url:
        if return_error:
            return None, 'invalid_arn'
        return None
    ok, data = run_aws(
        'aws sqs get-queue-attributes '
        f'--queue-url {shlex.quote(url)} '
        '--attribute-names Policy --output json'
    )
    if not ok or not data:
        err = (data or {}).get('_error', 'aws_error') if isinstance(data, dict) else 'aws_error'
        if return_error:
            return None, err
        return None
    policy_str = (data.get('Attributes') or {}).get('Policy')
    if not policy_str or policy_str == 'None':
        if return_error:
            return None, 'no_policy'
        return None
    try:
        doc = json.loads(policy_str)
        if return_error:
            return doc, None
        return doc
    except json.JSONDecodeError:
        if return_error:
            return None, 'invalid_policy_json'
        return None


def enrich_sqs_queue_policies(notifications):
    """
    Para cada QueueConfiguration, busca policy da fila e extrai kms_keys (se houver).
    Retorna { queue_arn: { kms_keys: [...] } }.
    """
    notif = notifications or {}
    out = {}
    for q in notif.get('QueueConfigurations', []):
        arn = q.get('QueueArn', '')
        if not arn:
            continue
        doc = fetch_sqs_queue_policy_document(arn)
        if not doc:
            continue
        kms = kms_keys_from_queue_policy_document(doc)
        if kms:
            out[arn] = {'kms_keys': kms}
    return out


def get_sqs_kms_keys_for_queue(cfg, queue_arn):
    """kms_keys da fila: cache em sqs_queue_policies ou fetch live."""
    cached = (cfg.get('sqs_queue_policies') or {}).get(queue_arn, {})
    kms = cached.get('kms_keys')
    if kms:
        return kms
    doc = fetch_sqs_queue_policy_document(queue_arn)
    if doc:
        return kms_keys_from_queue_policy_document(doc)
    return []


def notifications_to_bp_vars(cfg):
    """Converte formato AWS de notifications para vars da BP."""
    n = cfg.get('notifications') or {}
    result = {}

    # SQS
    sqs = n.get('QueueConfigurations', [])
    if sqs:
        sqs_map = {}
        for i, q in enumerate(sqs):
            key = f"notification_{i}"
            sqs_map[key] = {
                'queue_arn': q.get('QueueArn',''),
                'events': q.get('Events', []),
            }
            if q.get('Filter'):
                filt = q['Filter'].get('Key',{}).get('FilterRules',[])
                if filt:
                    prefix = next(
                        (f['Value'] for f in filt if f['Name'].lower()=='prefix'), None)
                    suffix = next(
                        (f['Value'] for f in filt if f['Name'].lower()=='suffix'), None)
                    if prefix: sqs_map[key]['filter_prefix'] = prefix
                    if suffix: sqs_map[key]['filter_suffix'] = suffix
            arn = q.get('QueueArn', '')
            kms_keys = get_sqs_kms_keys_for_queue(cfg, arn) if arn else []
            if kms_keys:
                sqs_map[key]['kms_keys'] = kms_keys
        result['sqs_notifications'] = sqs_map

    # SNS
    sns = n.get('TopicConfigurations', [])
    if sns:
        sns_map = {}
        for i, t in enumerate(sns):
            key = f"notification_{i}"
            sns_map[key] = {
                'topic_arn': t.get('TopicArn',''),
                'events': t.get('Events', []),
            }
        result['sns_notifications'] = sns_map

    # Lambda
    lambdas = n.get('LambdaFunctionConfigurations', [])
    if lambdas:
        lam_map = {}
        for i, l in enumerate(lambdas):
            key = f"notification_{i}"
            lambda_arn = l.get('LambdaFunctionArn','')
            lam_map[key] = {
                'function_arn': lambda_arn,  # usado em aws_s3_bucket_notification (linha 26 BP)
                'function_name': lambda_arn,  # usado em aws_lambda_permission (linha 70 BP)
                'events': l.get('Events', []),
            }
            # filter_prefix/suffix — mesma lógica das SQS (case insensitive)
            if l.get('Filter'):
                filt = l['Filter'].get('Key',{}).get('FilterRules',[])
                prefix = next((f['Value'] for f in filt if f['Name'].lower()=='prefix'), None)
                suffix = next((f['Value'] for f in filt if f['Name'].lower()=='suffix'), None)
                if prefix: lam_map[key]['filter_prefix'] = prefix
                if suffix: lam_map[key]['filter_suffix'] = suffix
        result['lambda_notifications'] = lam_map

    # EventBridge
    eb = n.get('EventBridgeConfiguration', {})
    if eb:
        result['eventbridge'] = True

    return result

def cors_to_bp_var(cfg):
    """Converte CORS AWS para formato da BP."""
    cors = cfg.get('cors') or {}
    rules = cors.get('CORSRules', [])
    if not rules:
        return []
    result = []
    for rule in rules:
        r = {
            'allowed_methods': rule.get('AllowedMethods', []),
            'allowed_origins': rule.get('AllowedOrigins', []),
        }
        if rule.get('AllowedHeaders'):
            r['allowed_headers'] = rule['AllowedHeaders']
        if rule.get('ExposeHeaders'):
            r['expose_headers'] = rule['ExposeHeaders']
        if rule.get('MaxAgeSeconds'):
            r['max_age_seconds'] = rule['MaxAgeSeconds']
        result.append(r)
    return result

def replication_to_bp_var(cfg):
    """Converte replication AWS para formato da BP."""
    rep = cfg.get('replication') or {}
    rc = rep.get('ReplicationConfiguration', {})
    if not rc:
        return {}
    return {
        'role': rc.get('Role',''),
        'rules': rc.get('Rules', []),
    }

def get_metrics_info(cfg):
    """Retorna (has_default_metric, custom_metrics_list)."""
    metrics_data = cfg.get('metrics') or {}
    metrics_list = metrics_data.get('MetricsConfigurationList', [])
    has_default = any(m.get('Id') == 'Default' for m in metrics_list)
    custom_metrics = [m for m in metrics_list if m.get('Id') != 'Default']
    return has_default, custom_metrics

def get_custom_tags(cfg, standard_keys):
    """Retorna tags extras além das padrão do módulo tags."""
    tags = (cfg.get('tagging') or {}).get('TagSet', [])
    extras = {}
    for tag in tags:
        if tag['Key'].lower() not in {k.lower() for k in standard_keys}:
            extras[tag['Key']] = tag['Value']
    return extras

# ── CLI standalone ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bucket', default=None)
    parser.add_argument('--csv', default=None)
    parser.add_argument('--output-dir', default='./s3configs')
    parser.add_argument('--parallel', type=int, default=5)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--max-cache-age', type=int, default=None, metavar='HORAS',
                        help='Re-extrai se cache tiver mais de N horas (padrão: sem limite)')
    args = parser.parse_args()

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    if args.bucket:
        buckets = [args.bucket]
    elif args.csv:
        with open(args.csv) as f:
            sep = ';' if f.read(1000).count(';') > 3 else ','
        with open(args.csv) as f:
            reader = csv.DictReader(f, delimiter=sep)
            buckets = [r.get('bucket_name','').strip() for r in reader
                       if r.get('bucket_name','').strip()]
    else:
        print("Use --bucket ou --csv"); sys.exit(1)

    print(f"Extraindo configurações completas de {len(buckets)} buckets...")

    counts = {'EXTRACTED':0,'CACHED':0,'EXTRACTED_WITH_ERRORS':0}
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {ex.submit(extract_bucket, b, args.output_dir, args.force, args.max_cache_age): b
                   for b in buckets}
        for future in as_completed(futures):
            b = futures[future]
            status, cfg = future.result()
            counts[status] = counts.get(status,0) + 1
            errors = cfg.get('_errors', {})
            icon = '✅' if status=='CACHED' else ('⚠️' if errors else '✅')
            err_str = f" [{','.join(errors.keys())}]" if errors else ''
            print(f" {icon} {b} [{status}]{err_str}")

    print(f"\nExtraídos: {counts.get('EXTRACTED',0)}")
    print(f"Cache: {counts.get('CACHED',0)}")
    print(f"Com erros: {counts.get('EXTRACTED_WITH_ERRORS',0)}")

if __name__ == '__main__':
    main()


# ══════════════════════════════════════════════════════════════════
# DIFF SEMÂNTICO — compara estado atual AWS vs estado desejado BP
# ══════════════════════════════════════════════════════════════════

def normalize_tag_key(k):
    """Normaliza chave de tag para comparação case-insensitive."""
    return k.lower().strip()

def normalize_tag_value(v):
    """Normaliza valor de tag — trim, uppercase para campos fixos."""
    v = str(v).strip()
    # CostString sempre uppercase
    if '.' in v and v.replace('.','').replace('BR','').isdigit():
        return v.upper()
    return v

def compute_semantic_diff(cfg, bucket_name, team, env, asset_cat,
                          ticket, log_bucket, appid_map=None):
    """
    Compara o estado atual do bucket (cfg extraído da AWS) com o estado
    desejado pela Blueprint S3.
    Retorna lista de dicts com as diferenças reais:
    {
      'campo': str, # ex: "tags.Repository"
      'atual': str, # valor atual na AWS
      'desejado': str, # valor que a BP vai aplicar
      'impacto': str, # CRITICO | ALTO | MEDIO | BAIXO | INFO
      'acao': str, # o que o apply vai fazer
      'bloquear_mr': bool, # se True, não abrir MR sem revisão humana
    }
    """
    if appid_map is None:
        appid_map = {
            'score':'7299','affinity':'7301','lno':'8745','ctools':'10892',
            'cross':'11151','kba':'111305','premium':'11969','antifraude':'11969',
            'ewallet':'12790','ipaas':'17510','chatbot':'17550','platform':'12810',
            'splunk':'19926','core':'11151','auth':'8547','partnersetting':'8239',
            'partners':'16878','financialservices':'16996','dynatrace':'12028',
            'grafana':'25180','datadog':'19647','zabbix':'25786','easyflow':'26446',
        }

    diffs = []
    tags = {t['Key']: t['Value'] for t in (cfg.get('tagging') or {}).get('TagSet', [])}

    def add(campo, atual, desejado, impacto='BAIXO', acao='', bloquear=False):
        if str(atual).strip() != str(desejado).strip():
            diffs.append({
                'campo': campo, 'atual': str(atual), 'desejado': str(desejado),
                'impacto': impacto, 'acao': acao or f'{atual} → {desejado}',
                'bloquear_mr': bloquear,
            })

    # ── 1. TAGS ───────────────────────────────────────────────────────────────
    import re as _re
    logical = bucket_name
    prefix = f"ecs-{team}-"
    suffix = f"-{env}"
    if bucket_name.startswith(prefix) and bucket_name.endswith(suffix):
        logical = bucket_name[len(prefix):-len(suffix)]

    repo_desejado = f"ecs-{team}-default-aws-terraform"
    name_desejado = bucket_name  # BP usa o nome do bucket como Name tag

    # Repository
    repo_atual = tags.get('Repository', tags.get('repository', ''))
    add('tags.Repository', repo_atual, repo_desejado, 'MEDIO',
        f'"{repo_atual}" → "{repo_desejado}" (migração para novo repo)')

    # Name
    name_atual = tags.get('Name', tags.get('name', ''))
    if name_atual and name_atual != name_desejado:
        add('tags.Name', name_atual, name_desejado, 'INFO',
            f'Atualiza Name tag para o nome real do bucket')

    # Group
    group_atual = tags.get('Group', tags.get('group', ''))
    if not group_atual:
        add('tags.Group', '(ausente)', 'ecs', 'BAIXO', 'Adiciona tag Group=ecs (padrão BP)')

    # CostString capitalização
    cost_atual = tags.get('CostString', tags.get('coststring', ''))
    cost_desejado = '1800.BR.208.604514'
    if cost_atual and cost_atual.upper() == cost_desejado and cost_atual != cost_desejado:
        add('tags.CostString', cost_atual, cost_desejado, 'INFO',
            'Capitalização: BR maiúsculo (cosmético)')

    # Ticket
    ticket_atual = tags.get('Ticket', tags.get('ticket', ''))
    if ticket and ticket != 'PREENCHER' and ticket_atual != ticket:
        add('tags.Ticket', ticket_atual or '(ausente)', ticket, 'INFO',
            f'Atualiza ticket para MR atual')

    # AppID
    appid_atual = tags.get('AppID', tags.get('appid', ''))
    appid_desejado = appid_map.get(team, '')
    if appid_desejado and appid_atual and appid_atual != appid_desejado:
        add('tags.AppID', appid_atual, appid_desejado, 'MEDIO',
            f'AppID do time {team}', bloquear=True)

    # data_type — obrigatório pela BP, valores válidos: PP, LP, PP/LP, N/A
    VALID_DATA_TYPE = {'PP', 'LP', 'PP/LP', 'N/A'}
    data_type_atual = tags.get('Data_Type', tags.get('data_type', ''))
    if not data_type_atual:
        diffs.append({
            'campo': 'tags.data_type',
            'atual': '(ausente)',
            'desejado': 'N/A (default)',
            'impacto': 'MEDIO',
            'acao': 'Tag ausente no bucket — BP vai usar N/A. Validar se correto.',
            'bloquear_mr': False,
        })
    elif data_type_atual not in VALID_DATA_TYPE:
        diffs.append({
            'campo': 'tags.data_type',
            'atual': data_type_atual,
            'desejado': f'Um de: {", ".join(sorted(VALID_DATA_TYPE))}',
            'impacto': 'ALTO',
            'acao': f'Valor inválido "{data_type_atual}" — BP vai rejeitar na validação',
            'bloquear_mr': True,
        })

    # data_category — obrigatório pela BP
    VALID_DATA_CAT = {'Registry', 'Behavioral', 'Negative', 'Positive', 'Financial', 'N/A'}
    data_cat_atual = tags.get('Data_Category', tags.get('data_category', ''))
    if not data_cat_atual:
        diffs.append({
            'campo': 'tags.data_category',
            'atual': '(ausente)',
            'desejado': 'N/A (default)',
            'impacto': 'MEDIO',
            'acao': 'Tag ausente — BP vai usar N/A. Validar se correto.',
            'bloquear_mr': False,
        })
    elif data_cat_atual not in VALID_DATA_CAT:
        diffs.append({
            'campo': 'tags.data_category',
            'atual': data_cat_atual,
            'desejado': f'Um de: {", ".join(sorted(VALID_DATA_CAT))}',
            'impacto': 'ALTO',
            'acao': f'Valor inválido "{data_cat_atual}" — BP vai rejeitar na validação',
            'bloquear_mr': True,
        })

    # ── 2. LIFECYCLE ──────────────────────────────────────────────────────────
    lc_rules = (cfg.get('lifecycle') or {}).get('Rules', [])
    lc_ids = {r.get('ID','') for r in lc_rules}

    # IDs esperados pela BP
    expected_ids = {'Padrao'}
    if env in ('dev','hml') and asset_cat != 'Cache':
        expected_ids.add('Expira em 6 meses dev/hml')
    if asset_cat in ('Productive data','Model development','Metadata','Embbeded'):
        expected_ids.add('90 StandardIA -> 180 Glacier')
    elif asset_cat in ('Development','Staging','Sandbox'):
        expected_ids.add('30 StandardIA -> 90 Glacier')
    elif asset_cat in ('Logs','Backup'):
        expected_ids.add('30 Glacier')
    elif asset_cat == 'Cache':
        expected_ids.add('Expira em 45 dias')

    missing = expected_ids - lc_ids
    extra = lc_ids - expected_ids

    if missing:
        diffs.append({
            'campo': 'lifecycle.rules_faltando',
            'atual': ', '.join(sorted(lc_ids)) or '(sem lifecycle)',
            'desejado': ', '.join(sorted(expected_ids)),
            'impacto': 'ALTO',
            'acao': f'BP vai ADICIONAR regras: {", ".join(sorted(missing))}',
            'bloquear_mr': False,
        })
    if extra:
        diffs.append({
            'campo': 'lifecycle.rules_customizadas',
            'atual': ', '.join(sorted(extra)),
            'desejado': '(não esperado pelo padrão BP)',
            'impacto': 'MEDIO',
            'acao': f'Regras customizadas presentes — serão mantidas explicitamente no main.tf',
            'bloquear_mr': False,
        })

    # ── 3. ENCRYPTION ─────────────────────────────────────────────────────────
    enc_rules = (cfg.get('encryption') or {}).get(
        'ServerSideEncryptionConfiguration', {}).get('Rules', [])
    enc_algo = enc_rules[0].get('ApplyServerSideEncryptionByDefault', {}).get(
        'SSEAlgorithm', '') if enc_rules else ''

    if enc_algo == 'AES256':
        diffs.append({
            'campo': 'encryption.algorithm',
            'atual': 'AES256',
            'desejado': 'AES256 (preservado via sse_algorithm override)',
            'impacto': 'INFO',
            'acao': 'sse_algorithm="AES256" explícito no main.tf — sem mudança',
            'bloquear_mr': False,
        })

    # ── 4. LOGGING ────────────────────────────────────────────────────────────
    log_target, log_prefix = get_logging_info(cfg)
    if log_target and log_target != log_bucket:
        diffs.append({
            'campo': 'logging.target_bucket',
            'atual': log_target,
            'desejado': log_bucket,
            'impacto': 'MEDIO',
            'acao': f'BP vai alterar destino do logging para {log_bucket}',
            'bloquear_mr': False,
        })

    # ── 5. NOTIFICATIONS ──────────────────────────────────────────────────────
    notif = cfg.get('notifications') or {}
    sqs = notif.get('QueueConfigurations', [])
    for q in sqs:
        filt = q.get('Filter',{}).get('Key',{}).get('FilterRules',[])
        prefix_val = next((f['Value'] for f in filt if f['Name'].lower()=='prefix'), None)
        if prefix_val:
            diffs.append({
                'campo': f'notifications.sqs.filter_prefix',
                'atual': prefix_val,
                'desejado': prefix_val,
                'impacto': 'INFO',
                'acao': f'filter_prefix="{prefix_val}" preservado na notification SQS',
                'bloquear_mr': False,
            })
    for arn, meta in (cfg.get('sqs_queue_policies') or {}).items():
        kms = meta.get('kms_keys') or []
        if kms:
            diffs.append({
                'campo': 'notifications.sqs.kms_keys',
                'atual': f'{len(kms)} CMK(s) na policy da fila',
                'desejado': 'kms_keys no main.tf (BP ≥2.2.1)',
                'impacto': 'INFO',
                'acao': f'KMSAllows preservado ({arn.split(":")[-1]})',
                'bloquear_mr': False,
            })

    # ── 6. CONFIGS NÃO SUPORTADAS ─────────────────────────────────────────────
    not_sup = get_not_supported(cfg)
    for ns_config, ns_risk in not_sup:
        impacto = 'CRITICO' if 'CRÍTICO' in ns_risk else 'MEDIO' if 'MÉDIO' in ns_risk else 'BAIXO'
        diffs.append({
            'campo': f'nao_suportado.{ns_config}',
            'atual': 'configurado',
            'desejado': 'lifecycle ignore_changes (BP não gerencia)',
            'impacto': impacto,
            'acao': ns_risk,
            'bloquear_mr': impacto == 'CRITICO',
        })

    return diffs

# Explicações amigáveis de cada campo
FIELD_EXPLAIN = {
    'tags.Repository': 'Repo do time onde o bucket está sendo gerenciado',
    'tags.Group': 'Tag obrigatória da BP (sempre "ecs")',
    'tags.Name': 'Nome do bucket como tag (padrão BP usa o nome real)',
    'tags.Ticket': 'Ticket da MR que criou/alterou o recurso',
    'tags.AppID': 'ID do sistema no catálogo de aplicações',
    'tags.data_type': 'Tipo de dado (PP=Pessoa Física, LP=Pessoa Jurídica, PP/LP, N/A) — obrigatório pela BP',
    'tags.data_category': 'Categoria do dado (Registry/Behavioral/Negative/Positive/Financial/N/A) — obrigatório pela BP',
    'tags.CostString': 'Centro de custo (formatação)',
    'lifecycle.rules_faltando': 'Regras de ciclo de vida que a BP precisa adicionar',
    'lifecycle.rules_customizadas': 'Regras de lifecycle fora do padrão BP — serão mantidas',
    'encryption.algorithm': 'Algoritmo de criptografia do bucket',
    'logging.target_bucket': 'Bucket de destino dos logs de acesso',
    'notifications.sqs.filter_prefix': 'Filtro de prefixo da notification SQS — sendo preservado',
    'notifications.sqs.kms_keys': 'CMKs da policy SQS (SSE-KMS) — emitidas como kms_keys na BP notification',
    'nao_suportado.inventory': 'Inventário automático (Cyera/Wiz) — configurado pela equipe de segurança, a BP não gerencia e não vai remover',
    'nao_suportado.object_lock': 'Object Lock — proteção contra deleção, a BP não gerencia',
    'nao_suportado.website': 'Static website hosting — a BP não gerencia',
    'nao_suportado.custom_metrics': 'Métricas customizadas — a BP só suporta a métrica "Default"',
}

def format_diff_report(diffs, bucket_name):
    """Formata o diff para exibição no terminal e no log."""
    if not diffs:
        return f" ✅ {bucket_name} — bucket já está conforme a Blueprint"

    order = {'CRITICO':0,'ALTO':1,'MEDIO':2,'BAIXO':3,'INFO':4}
    icons = {'CRITICO':'🔴','ALTO':'🟠','MEDIO':'🟡','BAIXO':'🟢','INFO':'ℹ️ '}
    sorted_diffs = sorted(diffs, key=lambda x: order.get(x['impacto'],9))

    # Separa por relevância
    relevant = [d for d in sorted_diffs if d['impacto'] not in ('INFO',)
                and d['campo'] not in ('nao_suportado.inventory','nao_suportado.analytics',
                                       'encryption.algorithm','notifications.sqs.filter_prefix',
                                       'notifications.sqs.kms_keys')]
    info = [d for d in sorted_diffs if d not in relevant]

    lines = []

    # Resumo de lifecycle compliance
    lc_ok = not any(d['campo'] == 'lifecycle.rules_faltando' for d in diffs)
    lines.append(f" 📊 {bucket_name}")
    lines.append(f" Lifecycle mínimo BP: {'✅ OK' if lc_ok else '⚠️ FALTANDO REGRAS'}")

    if relevant:
        lines.append(f" Mudanças que vão ocorrer no apply ({len(relevant)}):")
        for d in relevant:
            icon = icons.get(d['impacto'], ' ')
            bloquear = ' ⛔ REVISAR ANTES DE APROVAR' if d.get('bloquear_mr') else ''
            explain = FIELD_EXPLAIN.get(d['campo'], '')
            lines.append(f" {icon} {d['campo']}{bloquear}")
            if explain:
                lines.append(f" 📌 {explain}")
            lines.append(f" → {d['acao']}")
    else:
        lines.append(f" Mudanças relevantes: ✅ nenhuma")

    if info:
        # Agrupa notifications para não poluir
        notif_infos = [d for d in info if 'filter_prefix' in d['campo']]
        other_infos = [d for d in info if 'filter_prefix' not in d['campo']]

        if other_infos or notif_infos:
            lines.append(f" Informações (sem impacto no apply):")
            for d in other_infos:
                explain = FIELD_EXPLAIN.get(d['campo'], '')
                lines.append(f" ℹ️ {d['campo']}: {d['acao']}")
                if explain:
                    lines.append(f" 📌 {explain}")
            if notif_infos:
                lines.append(f" ℹ️ notifications: {len(notif_infos)} filter_prefix(es) preservados")
                lines.append(f" 📌 Filtros de prefixo de notifications (SQS/Lambda) — sendo preservados")
                for d in notif_infos:
                    lines.append(f" • {d['atual']}")

    bloqueantes = [d for d in diffs if d.get('bloquear_mr')]
    if bloqueantes:
        lines.append(f"")
        lines.append(f" ⛔ ATENÇÃO: {len(bloqueantes)} item(s) precisam revisão antes de aprovar:")
        for b in bloqueantes:
            lines.append(f" • {b['campo']}: {b['acao']}")

    return '\n'.join(lines)

def needs_mr_from_diff(diffs):
    """Determina se o diff justifica abrir uma MR."""
    if not diffs:
        return False, 'Sem diferenças reais'

    # Campos que não justificam MR sozinhos
    SKIP_FIELDS = {
        'nao_suportado.inventory',
        'nao_suportado.analytics',
        'encryption.algorithm',
        'notifications.sqs.filter_prefix',
    }
    relevant = [d for d in diffs
                if d['impacto'] not in ('INFO',)
                and d['campo'] not in SKIP_FIELDS]

    if not relevant:
        return False, 'Só mudanças cosméticas/infra — sem MR necessária'
    return True, f'{len(relevant)} diferença(s) relevante(s)'

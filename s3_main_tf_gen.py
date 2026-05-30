#!/usr/bin/env python3
"""
s3_main_tf_gen.py
Gera main.tf, CHANGES.md e import_commands.sh para um bucket S3.
Usa o config completo extraído pelo s3_config_extractor.py.
Importado pelo s3_migrate.py.
"""

import json, re
from datetime import datetime
from pathlib import Path

# Importa helpers do extractor
import sys
sys.path.insert(0, str(Path(__file__).parent))
from s3_config_extractor import (
    has_notifications, get_kms_key_id, get_not_supported,
    get_public_access_warnings, get_logging_info, get_ownership,
    notifications_to_bp_vars, cors_to_bp_var, replication_to_bp_var,
    get_custom_tags, get_metrics_info
)

BP_SOURCE = "git::https://gitlab.ecsbr.net/ecs/engineering/ecs-engineering-terraform-blueprint-aws-s3.git?ref=2"
DEFAULT_LOG_BUCKET = "ecs-387979423286-logging-s3"

# Configs não suportadas: atributos de aws_s3_bucket que precisam de ignore_changes
_BUCKET_ATTR_IGNORE_CONFIGS = {'website_configuration', 'accelerate_configuration', 'request_payment'}
# Configs não suportadas: recursos separados — só não importar, plan não propõe destroy
_SEPARATE_NO_IMPORT_CONFIGS = {'object_lock', 'intelligent_tiering', 'inventory', 'analytics', 'custom_metrics'}
# Ordem de severidade para cálculo de risco
_NS_RISK_ORDER = {'CRÍTICO': 4, 'ALTO': 3, 'MÉDIO': 2, 'BAIXO': 1}

def _ns_max_level(not_sup_list):
    """Retorna o score máximo de severidade em not_sup."""
    best = 0
    for _, risk_text in not_sup_list:
        for word, score in _NS_RISK_ORDER.items():
            if word in risk_text:
                best = max(best, score)
    return best

APPID_MAP = {
    'score': '7299', 'affinity': '7301', 'lno': '8745',
    'ctools': '10892', 'cross': '11151', 'kba': '111305',
    'premium': '11969', 'antifraude': '11969', 'ewallet': '12790',
    'ipaas': '17510', 'chatbot': '17550', 'platform': '12810',
    'splunk': '19926', 'core': '11151', 'auth': '8547',
    'partnersetting': '8239', 'partners': '16878',
    'financialservices': '16996', 'dynatrace': '12028',
    'grafana': '25180', 'datadog': '19647', 'zabbix': '25786',
    'easyflow': '26446',
}
FIXED_BUSINESSSERVICES = "Serasa Consumidor"
FIXED_COSTSTRING = "1800.BR.208.604514"

# BP tags module — valores exatos (registry.terraform.io validation)
BP_VALID_ASSET_CATEGORIES = {
    'Productive data', 'Development', 'Staging', 'Sandbox',
    'Model development', 'Logs', 'Embbeded', 'Metadata', 'Cache', 'Backup',
}
_ASSET_CATEGORY_ALIASES = {
    'Model Development': 'Model development',
    'model development': 'Model development',
}


def normalize_asset_category(cat):
    """Corrige typos do CSV/AWS para valores aceitos pela BP."""
    c = (cat or '').strip()
    if c in BP_VALID_ASSET_CATEGORIES:
        return c
    return _ASSET_CATEGORY_ALIASES.get(c, c)


STANDARD_TAG_KEYS = {
    'application','product','environment','team','ticket','appid',
    'businessservices','coststring','asset_category','data_type',
    'data_category','repository','blueprint','name',
    'Application','Product','Environment','Team','Ticket','Repository',
    'Blueprint','Asset_Category','Data_Type','Data_Category',
    'Appid','Businessservices','Coststring',
}

def get_tag(cfg, *keys):
    """Extrai a primeira tag encontrada dentre as chaves fornecidas."""
    tags = (cfg.get('tagging') or {}).get('TagSet', [])
    for key in keys:
        for tag in tags:
            if tag.get('Key','').lower() == key.lower():
                v = tag.get('Value','').strip()
                if v:
                    return v
    return ''

def resolve_tags(bucket_name, team, env, asset_cat_csv, cfg, ticket='PREENCHER'):
    logical, _ = extract_logical(bucket_name, team, env)
    tags = {}
    warnings = []

    tags['application'] = get_tag(cfg, 'Application', 'application') or logical
    tags['product'] = get_tag(cfg, 'Product', 'product') or team
    tags['environment'] = env
    tags['team'] = team
    tags['ticket'] = ticket
    tags['appid'] = (get_tag(cfg, 'Appid', 'appid') or
                     APPID_MAP.get(team, '') or 'PREENCHER')
    tags['businessservices'] = FIXED_BUSINESSSERVICES
    tags['coststring'] = FIXED_COSTSTRING
    raw_asset = (get_tag(cfg, 'Asset_Category', 'asset_category') or
                 asset_cat_csv or 'PREENCHER')
    tags['asset_category'] = normalize_asset_category(raw_asset)
    # data_type e data_category: N/A é válido pela BP quando não configurado no bucket
    # PREENCHER causa erro de validação na pipeline — usar N/A como fallback seguro
    tags['data_type'] = get_tag(cfg, 'Data_Type', 'data_type') or 'N/A'
    tags['data_category'] = get_tag(cfg, 'Data_Category', 'data_category') or 'N/A'
    tags['repository'] = f"ecs-{team}-default-aws-terraform"

    if tags['asset_category'] == 'PREENCHER':
        warnings.append('asset_category não encontrada — preencher manualmente')
    # data_type e data_category usam N/A como default — sem warning necessário
    if tags['appid'] == 'PREENCHER':
        warnings.append(f'appid não mapeado para o time "{team}" — preencher manualmente')

    return tags, warnings

STANDARD_TAG_KEYS = {
    'application','product','environment','team','ticket','appid',
    'businessservices','coststring','asset_category','data_type',
    'data_category','group','repository','blueprint','name',
    'Environment','Team','Product','Application','Ticket','Repository',
    'Group','Blueprint','Asset_Category','Data_Type','Data_Category',
    'Appid','Businessservices','Coststring'
}

def bp_default_rule_ids(asset_category, env, versioning):
    """
    Retorna os IDs das regras que a BP aplicaria por padrão para esse bucket.
    Baseado no main.tf da Blueprint S3 ref=2.
    """
    ids = {'Padrao'} # Sempre presente
    cat = str(asset_category).strip()
    e = str(env).strip().lower()

    if e in ('dev','hml') and cat != 'Cache':
        ids.add('Expira em 6 meses dev/hml')

    if cat in ('Productive data','Model development','Metadata'):
        ids.add('90 StandardIA -> 180 Glacier')
    elif cat in ('Development','Staging','Sandbox'):
        ids.add('30 StandardIA -> 90 Glacier')
    elif cat in ('Logs','Backup'):
        ids.add('30 Glacier')
    elif cat == 'Cache':
        ids.add('Expira em 45 dias')

    if (
        versioning == 'Enabled'
        and e == 'prd'
        and cat in ('Productive data', 'Embbeded')
    ):
        ids.add('Deleta as 10 versoes nao atuais apos 180 dias')

    return ids

def lifecycle_matches_bp_default(lc_rules, asset_category, env, versioning):
    """
    Verifica se as regras extraídas do bucket coincidem com o que a BP
    aplicaria por padrão. Se sim, não precisa passar lifecycle_rules.
    """
    if not lc_rules:
        return True # Sem lifecycle → BP vai aplicar o padrão

    extracted_ids = {r.get('ID', r.get('id', '')) for r in lc_rules}
    bp_ids = bp_default_rule_ids(asset_category, env, versioning)

    # Se os IDs extraídos são um subconjunto dos padrão BP → não customizar
    # (a BP vai recriar as regras certas)
    return extracted_ids.issubset(bp_ids)


def has_lambda_notifications(cfg):
    n = cfg.get('notifications') or {}
    return bool(n.get('LambdaFunctionConfigurations'))


def lifecycle_transition_drift_warnings(lc_rules, asset_category, env):
    """
    IDs de lifecycle batem com BP, mas transições na AWS podem sumir no plan
    quando main.tf não declara lifecycle_rules (BP implícita).
    """
    warnings = []
    if not lc_rules:
        return warnings
    cat = str(asset_category).strip()
    e = str(env).strip().lower()

    for rule in lc_rules:
        rid = rule.get('ID', rule.get('id', ''))
        for t in rule.get('Transitions') or []:
            days = t.get('Days')
            sc = (t.get('StorageClass') or '')
            sc_u = sc.upper()

            if (
                rid == '30 StandardIA -> 90 Glacier'
                and cat in ('Development', 'Staging', 'Sandbox')
                and days == 90
                and 'GLACIER' in sc_u
            ):
                warnings.append(
                    f"Regra `{rid}`: plan típico **remove** transição {days}d→`{sc}` "
                    f"(BP implícita sem `lifecycle_rules` no main.tf)"
                )
            if (
                rid == '90 StandardIA -> 180 Glacier'
                and cat in ('Productive data', 'Model development', 'Metadata')
                and days == 180
                and 'GLACIER' in sc_u
            ):
                warnings.append(
                    f"Regra `{rid}`: plan típico **remove** transição {days}d→`{sc}` "
                    f"(validar BP vs AWS antes do merge)"
                )
            if (
                rid == '30 Glacier'
                and cat in ('Logs', 'Backup')
                and days == 120
                and sc_u == 'GLACIER'
            ):
                warnings.append(
                    f"Regra `{rid}`: AWS com 120d→GLACIER — BP governança usa **90d→GLACIER** "
                    f"(plan pode ajustar após BP ≥2.2.1)"
                )
    return warnings


def estimate_plan_expectations(cfg, lc_mode, lc_drift_warnings, has_ownership_aws):
    """Heurística do plan pós-import (sem rodar terraform)."""
    adds = []
    changes = []

    if has_lambda_notifications(cfg):
        adds.append((
            '+ create',
            'module.s3.module.notification.aws_lambda_permission.allow[...]',
            'Permissão S3→Lambda (notification já existe na AWS)',
        ))
    if not has_ownership_aws:
        adds.append((
            '+ create',
            'module.s3.aws_s3_bucket_ownership_controls.main',
            'BP define `BucketOwnerEnforced`',
        ))
    if lc_mode == 'bp_new':
        adds.append((
            '+ create',
            'module.s3.aws_s3_bucket_lifecycle_configuration.main',
            'Lifecycle padrão BP (sem LC na AWS hoje)',
        ))

    changes.append((
        '~ update',
        'module.s3.aws_s3_bucket.main',
        'Tags BP (`Name`, `Ticket`, `AppliedAt`, `Repository`, `Group`, etc.)',
    ))
    changes.append((
        '~ update',
        'module.s3.aws_s3_bucket_logging.main',
        '`target_object_key_format` (partitioned prefix `EventTime`)',
    ))
    if has_notifications(cfg):
        changes.append((
            '~ update',
            'module.s3.module.notification.aws_s3_bucket_notification.main[0]',
            'Renomeio de chaves Terraform (ex.: id da Lambda no state)',
        ))
    if any(
        (meta.get('kms_keys') or [])
        for meta in (cfg.get('sqs_queue_policies') or {}).values()
    ):
        changes.append((
            '~ update',
            'module.s3.module.notification.aws_sqs_queue_policy.allow[...]',
            '`kms_keys` no HCL preserva statement KMSAllows (SSE-KMS) — requer BP ≥2.2.1',
        ))
    for w in lc_drift_warnings:
        changes.append((
            '~ update',
            'module.s3.aws_s3_bucket_lifecycle_configuration.main',
            w,
        ))
    if (
        not lc_drift_warnings
        and lc_mode == 'bp_match'
        and (cfg.get('lifecycle') or {}).get('Rules')
    ):
        changes.append((
            '~ update',
            'module.s3.aws_s3_bucket_lifecycle_configuration.main',
            'Conferir drift em transitions/filters no plan real',
        ))

    n_add = len(adds)
    n_chg = len(changes)
    summary = (
        f"**Plan esperado (heurística):** `{n_add} to add`, `{n_chg} to change`, `0 destroy` "
        f"— validar na pipeline"
    )
    return adds, changes, summary


def extract_logical(bucket_name, team, env):
    prefix = f"ecs-{team}-"
    suffix = f"-{env}"
    if bucket_name.startswith(prefix) and bucket_name.endswith(suffix):
        return bucket_name[len(prefix):-len(suffix)], True
    return re.sub(r'^ecs-', '', bucket_name), False

def val_hcl(v):
    if isinstance(v, bool): return 'true' if v else 'false'
    if isinstance(v, str): return f'"{v}"'
    if isinstance(v, int): return str(v)
    if isinstance(v, list): return json.dumps(v)
    if isinstance(v, dict): return json.dumps(v)
    return str(v)

def lifecycle_rule_to_hcl(rules_list, indent=2):
    """Converte lista de regras AWS lifecycle → bloco HCL lifecycle_rules."""
    pad = ' ' * indent
    pad2 = ' ' * (indent + 2)
    pad3 = ' ' * (indent + 4)

    def render(obj, depth=0):
        p = ' ' * (indent + 4 + depth * 2)
        if isinstance(obj, dict):
            lines = ['{']
            for k, v in obj.items():
                lines.append(f'{p}{k} = {render(v, depth+1)},')
            lines.append(' ' * (indent + 2 + depth * 2) + '}')
            return '\n'.join(lines)
        if isinstance(obj, list):
            if not obj: return '[]'
            lines = ['[']
            for item in obj:
                lines.append(f'{p}{render(item, depth+1)},')
            lines.append(' ' * (indent + 2 + depth * 2) + ']')
            return '\n'.join(lines)
        return val_hcl(obj)

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
                if 'ObjectSizeGreaterThan' in a: fout['object_size_greater_than'] = a['ObjectSizeGreaterThan']
                if 'ObjectSizeLessThan' in a: fout['object_size_less_than'] = a['ObjectSizeLessThan']
            if fout: out['filter'] = fout
        exp = rule.get('Expiration', {})
        if exp:
            eout = {}
            if 'Days' in exp: eout['days'] = exp['Days']
            if 'Date' in exp: eout['date'] = exp['Date']
            if 'ExpiredObjectDeleteMarker' in exp:
                eout['expired_object_delete_marker'] = exp['ExpiredObjectDeleteMarker']
            if eout: out['expiration'] = eout
        trans = rule.get('Transitions', [])
        if trans:
            out['transition'] = [
                {k: v for k,v in {'days':t.get('Days'),'date':t.get('Date'),
                'storage_class':t.get('StorageClass')}.items() if v is not None}
                for t in trans
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
        aimu = rule.get('AbortIncompleteMultipartUpload', {})
        if aimu and 'DaysAfterInitiation' in aimu:
            out['abort_incomplete_multipart_upload'] = {'days_after_initiation': aimu['DaysAfterInitiation']}
        return out

    hcl = [f'{pad}lifecycle_rules = [']
    for rule in rules_list:
        bp_rule = aws_rule_to_bp(rule)
        hcl.append(f'{pad2}{{')
        for k, v in bp_rule.items():
            hcl.append(f'{pad3}{k} = {render(v)},')
        hcl.append(f'{pad2}}},')
    hcl.append(f'{pad}]')
    return '\n'.join(hcl)

def cors_to_hcl(cors_rules, indent=2):
    pad = ' ' * indent
    pad2 = ' ' * (indent + 2)
    pad3 = ' ' * (indent + 4)
    hcl = [f'{pad}cors_rules = [']
    for rule in cors_rules:
        hcl.append(f'{pad2}{{')
        for k, v in rule.items():
            hcl.append(f'{pad3}{k} = {val_hcl(v)},')
        hcl.append(f'{pad2}}},')
    hcl.append(f'{pad}]')
    return '\n'.join(hcl)

def notifications_to_hcl(notif_vars, indent=2):
    pad = ' ' * indent
    lines = []
    for var_name, var_val in notif_vars.items():
        if isinstance(var_val, bool):
            lines.append(f'{pad}{var_name} = {val_hcl(var_val)}')
        elif isinstance(var_val, dict):
            lines.append(f'{pad}{var_name} = {{')
            for k, v in var_val.items():
                # Omite chaves com valor None (ex: filter_prefix/suffix não configurados)
                if isinstance(v, dict):
                    # notification_0 = { queue_arn = ..., events = [...], filter_prefix = ... }
                    lines.append(f'{pad}  {k} = {{')
                    for nk, nv in v.items():
                        if nv is None:
                            continue # Omite filter_prefix/suffix quando None
                        lines.append(f'{pad}    {nk} = {json.dumps(nv, ensure_ascii=False)}')
                    lines.append(f'{pad}  }}')
                else:
                    lines.append(f'{pad}  {k} = {json.dumps(v, ensure_ascii=False)}')
            lines.append(f'{pad}}}')
    return '\n'.join(lines)

def aws_replication_rule_to_bp(rule):
    """Converte uma Rule da API GetBucketReplication para o formato da BP S3."""
    out = {}
    rid = rule.get('ID') or rule.get('id')
    if rid is not None:
        out['id'] = rid
    prio = rule.get('Priority')
    if prio is None:
        prio = rule.get('priority')
    if prio is not None:
        out['priority'] = prio
    st = rule.get('Status') or rule.get('status') or 'Enabled'
    out['status'] = st if isinstance(st, bool) else (st == 'Enabled')

    dmr = rule.get('DeleteMarkerReplication') or rule.get('delete_marker_replication')
    if dmr is not None:
        if isinstance(dmr, dict):
            dmr_st = dmr.get('Status') or dmr.get('status')
            out['delete_marker_replication'] = dmr_st == 'Enabled'
        else:
            out['delete_marker_replication'] = dmr

    ssc = rule.get('SourceSelectionCriteria') or rule.get('source_selection_criteria')
    if ssc:
        sse = ssc.get('SseKmsEncryptedObjects') or ssc.get('sse_kms_encrypted_objects') or {}
        if sse:
            sse_st = sse.get('Status') or sse.get('status') or sse.get('enabled')
            out['source_selection_criteria'] = {
                'sse_kms_encrypted_objects': {
                    'enabled': sse_st in (True, 'Enabled', 'enabled'),
                },
            }

    filt = rule.get('Filter') or rule.get('filter')
    if filt:
        prefix = filt.get('Prefix') if 'Prefix' in filt else filt.get('prefix')
        if prefix:
            out['filter'] = {'prefix': prefix}

    dest = rule.get('Destination') or rule.get('destination') or {}
    if dest:
        d = {}
        bucket = dest.get('Bucket') or dest.get('bucket')
        if bucket:
            d['bucket'] = bucket
        sc = dest.get('StorageClass') or dest.get('storage_class')
        if sc:
            d['storage_class'] = sc
        acct = dest.get('Account') or dest.get('account_id') or dest.get('account')
        if acct:
            d['account_id'] = str(acct)
        enc = dest.get('EncryptionConfiguration') or dest.get('encryption_configuration') or {}
        rk = enc.get('ReplicaKmsKeyID') or enc.get('replica_kms_key_id')
        if rk:
            d['replica_kms_key_id'] = rk
        act = dest.get('AccessControlTranslation') or dest.get('access_control_translation')
        if act:
            owner = act.get('Owner') or act.get('owner') or 'Destination'
            d['access_control_translation'] = {'owner': owner}
        out['destination'] = d

    return out


def replication_to_hcl(rep_config, indent=2):
    """HCL replication_configuration no formato esperado pelo blueprint (destination em minúsculas)."""
    if not rep_config:
        return ''
    pad = ' ' * indent
    pad2 = ' ' * (indent + 2)
    pad3 = ' ' * (indent + 4)
    pad4 = ' ' * (indent + 6)
    rules = rep_config.get('rules') or rep_config.get('Rules') or []
    bp_rules = [aws_replication_rule_to_bp(r) for r in rules]
    lines = [
        f'{pad}replication_configuration = {{',
        f'{pad}  role = {val_hcl(rep_config.get("role", ""))}',
        f'{pad}  rules = [',
    ]
    for rule in bp_rules:
        lines.append(f'{pad2}{{')
        for k, v in rule.items():
            if k == 'destination' and isinstance(v, dict):
                lines.append(f'{pad3}destination = {{')
                for dk, dv in v.items():
                    if dk == 'access_control_translation' and isinstance(dv, dict):
                        lines.append(f'{pad4}access_control_translation = {{')
                        lines.append(f'{pad4}  owner = {val_hcl(dv.get("owner", "Destination"))},')
                        lines.append(f'{pad4}}}')
                    else:
                        lines.append(f'{pad4}{dk} = {val_hcl(dv)},')
                lines.append(f'{pad3}}}')
            elif k == 'source_selection_criteria' and isinstance(v, dict):
                lines.append(f'{pad3}source_selection_criteria = {{')
                sse = v.get('sse_kms_encrypted_objects', {})
                if sse:
                    lines.append(f'{pad4}sse_kms_encrypted_objects = {{')
                    lines.append(f'{pad4}  enabled = {val_hcl(sse.get("enabled", True))},')
                    lines.append(f'{pad4}}}')
                lines.append(f'{pad3}}}')
            elif k == 'filter' and isinstance(v, dict):
                lines.append(f'{pad3}filter = {{')
                for fk, fv in v.items():
                    lines.append(f'{pad4}{fk} = {val_hcl(fv)},')
                lines.append(f'{pad3}}}')
            else:
                lines.append(f'{pad3}{k} = {val_hcl(v)},')
        lines.append(f'{pad2}}},')
    lines.append(f'{pad}]')
    lines.append(f'{pad}}}')
    return '\n'.join(lines)

def gen_main_tf(bucket_name, team, env, asset_cat, cfg,
                state_bucket, state_region, log_bucket=DEFAULT_LOG_BUCKET, ticket="PREENCHER"):
    """Gera o main.tf completo baseado no config extraído da AWS."""

    logical, follows_bp = extract_logical(bucket_name, team, env)
    repo_name = f"ecs-{team}-default-aws-terraform"
    state_key = f"{repo_name}/services/s3/{logical}/{env}/terraform.tfstate"

    # Analisa configurações
    enc = cfg.get('encryption') or {}
    enc_rules = enc.get('ServerSideEncryptionConfiguration', {}).get('Rules', [])
    enc_algo = ''
    if enc_rules:
        enc_algo = enc_rules[0].get('ApplyServerSideEncryptionByDefault',{}).get('SSEAlgorithm','')

    kms_key = get_kms_key_id(cfg)
    vers = (cfg.get('versioning') or {}).get('Status', '')
    lc_rules = (cfg.get('lifecycle') or {}).get('Rules', [])
    policy_json = (cfg.get('bucket_policy') or {}).get('Policy', '')
    cors_rules = cors_to_bp_var(cfg)
    notif_vars = notifications_to_bp_vars(cfg)
    rep_config = replication_to_bp_var(cfg)
    ownership = get_ownership(cfg)
    log_target, log_prefix = get_logging_info(cfg)
    not_supported = get_not_supported(cfg)
    pub_warnings = get_public_access_warnings(cfg)

    # Resolve todas as tags com prioridade: bucket existente → derivado → fixo
    tags, tag_warnings = resolve_tags(bucket_name, team, env, asset_cat, cfg, ticket)
    asset_cat_norm = tags['asset_category']

    lines = []

    # Comentário apenas para configs que geram ignore_changes em aws_s3_bucket.main
    _has_attr_ignore = any(ns in _BUCKET_ATTR_IGNORE_CONFIGS for ns, _ in not_supported)
    if _has_attr_ignore:
        lines += [
            f"# ⚠️ CONFIGURAÇÕES COM ignore_changes EM aws_s3_bucket.main",
            f"# Os atributos abaixo existem no bucket mas a BP não os gerencia.",
            f"# O bloco lifecycle.ignore_changes ao final impede que o plan proponha removê-los.",
        ]
        for ns_config, ns_risk in not_supported:
            if ns_config in _BUCKET_ATTR_IGNORE_CONFIGS:
                lines.append(f"# {ns_risk}")
        lines += [""]

    lines += [
        f"locals {{",
        f"  tags = {{",
        f'    application = "{tags["application"]}"',
        f'    product = "{tags["product"]}"',
        f'    environment = "{tags["environment"]}"',
        f'    team = "{tags["team"]}"',
        f'    ticket = "{tags["ticket"]}"',
        f'    appid = "{tags["appid"]}"',
        f'    businessservices = "{tags["businessservices"]}"',
        f'    coststring = "{tags["coststring"]}"',
        f'    asset_category = "{tags["asset_category"]}"',
        f'    data_type = "{tags["data_type"]}"',
        f'    data_category = "{tags["data_category"]}"',
        f'    repository = "{tags["repository"]}"',
        f'    group = "ecs"',
        f"  }}",
        f"}}",
        f"",
        f'module "s3" {{',
        f'  source = "{BP_SOURCE}"',
        f"",
    ]

    lines.append(f'  tags = local.tags')

    # tag_legacy_name — SEMPRE — garante que o nome não muda
    lines.append(f'  tag_legacy_name = "{bucket_name}"'
                 + ('' if follows_bp else ' # Nome fora do padrão BP — legacy_name obrigatório'))

    # Logging — BP deriva o prefix automaticamente como {account}/{bucket_name}/
    if log_target and log_target != log_bucket:
        lines.append(f'  logging_target_bucket = "{log_target}"'
                     f' # Destino diferente do padrão BP — mantido')
    else:
        lines.append(f'  logging_target_bucket = "{log_bucket}"')
    # Nota: o logging_target_prefix será sobrescrito pela BP para o padrão dela
    # ({account_id}/{bucket_name}/ com PartitionedPrefix EventTime)

    # Encryption
    if enc_algo == 'AES256':
        lines.append(f'  sse_algorithm = "AES256"'
                     f' # OVERRIDE: mantém AES256 — NÃO alterar para aws:kms')
    elif kms_key:
        lines.append(f'  sse_algorithm = "aws:kms"')
        lines.append(f'  kms_master_key_id = "{kms_key}"'
                     f' # CMK customizada — NÃO remover')

    # Versioning — BP só ativa em prd + (Productive data | Embbeded)
    if vers in ('Enabled', 'Suspended'):
        bp_will_manage = (env == 'prd' and asset_cat_norm in ('Productive data','Embbeded'))
        comment = '' if bp_will_manage else ' # ⚠️ BP só ativa em prd com Productive data/Embbeded'
        lines.append(f'  versioning_configuration = "{vers}"{comment}')

    # Object ownership (se diferente do padrão)
    if ownership != 'BucketOwnerEnforced':
        lines.append(f'  object_ownership = "{ownership}"'
                     f' # Diferente do padrão BP (BucketOwnerEnforced)')

    # Bucket metric — BP suporta apenas a métrica "Default"
    has_default_metric, custom_metrics = get_metrics_info(cfg)
    if has_default_metric:
        lines.append(f'  enable_bucket_metric = true'
                     f' # Métrica "Default" existente — preservada')

    # Public access block warning
    if pub_warnings:
        lines.append(f'  acl = "private" # ⚠️ BP vai ativar block public access')

    # Lifecycle — só injeta se for customizado (diferente do padrão BP)
    vers_status = (cfg.get('versioning') or {}).get('Status', '')
    is_bp_default = lifecycle_matches_bp_default(lc_rules, asset_cat_norm, env, vers_status)

    if lc_rules and not is_bp_default:
        lines += ["", f"  # Lifecycle CUSTOMIZADO extraído da AWS ({len(lc_rules)} regra(s))"]
        lines += ["  # (diferente do padrão BP — mantido explicitamente)"]
        lines.append(lifecycle_rule_to_hcl(lc_rules, indent=2))
    elif lc_rules and is_bp_default:
        lines += [
            "",
            f"  # Lifecycle: regras existentes coincidem com o padrão BP para",
            f"  # asset_category={asset_cat_norm} env={env} — BP aplica automaticamente.",
            f"  # NÃO é necessário passar lifecycle_rules.",
        ]
    else:
        lines += [
            "",
            f"  # Sem lifecycle configurado — BP vai criar baseado em asset_category={asset_cat_norm}",
        ]

    # Bucket policy — referencia o arquivo files/policy.json (padrão ECS)
    if policy_json:
        lines += [
            "",
            f"  # Bucket policy — arquivo em files/policy.json",
            f'  policy_json = file("${{path.module}}/files/policy.json")',
        ]

    # CORS
    if cors_rules:
        lines += ["", f"  # CORS extraído da AWS ({len(cors_rules)} regra(s))"]
        lines.append(cors_to_hcl(cors_rules, indent=2))

    # Notifications
    if notif_vars:
        lines += ["", f"  # Notifications extraídas da AWS"]
        lines.append(notifications_to_hcl(notif_vars, indent=2))

    # Replication
    if rep_config:
        lines += ["", f"  # Replication extraída da AWS — verificar antes do apply"]
        lines.append(replication_to_hcl(rep_config, indent=2))

    lines += [f"}}"]

    # lifecycle meta-argument para configs não suportadas
    BUCKET_ATTR_IGNORE = {
        'website_configuration': 'website',
        'accelerate_configuration': 'acceleration_status',
        'request_payment': 'request_payer',
    }
    SEPARATE_NO_IMPORT = {'object_lock','intelligent_tiering','inventory','analytics','custom_metrics'}
    ns_configs = {ns for ns, _ in not_supported}
    bucket_ignores = [BUCKET_ATTR_IGNORE[ns] for ns in ns_configs if ns in BUCKET_ATTR_IGNORE]

    # Bloco standalone aws_s3_bucket.main apenas para atributos que o BP gerencia
    # mas precisam de ignore_changes (website, acceleration_status, request_payer).
    # Para configs SEPARATE_NO_IMPORT (inventory, object_lock, etc.) não é necessário
    # ignore_changes — basta não importar (já tratado por _SKIP_IMPORT).
    if bucket_ignores:
        lines += [
            "",
            f"# Impede destroy de configs não gerenciadas pela BP",
            f'resource "aws_s3_bucket" "main" {{',
            f"  lifecycle {{",
            f"    ignore_changes = [",
        ]
        for attr in bucket_ignores:
            lines.append(f"      {attr},")
        lines += [f"    ]", f"  }}", f"}}"]

    lines.append("")
    return '\n'.join(lines)

_SKIP_IMPORT = {
    'aws_s3_bucket_object_lock_configuration',
    'aws_s3_bucket_intelligent_tiering_configuration',
    'aws_s3_bucket_inventory_configuration',
    'aws_s3_bucket_analytics_configuration',
}


def _bash_import_sqs_queue_policy_lines(tf_addr, queue_url):
    """Import aws_sqs_queue_policy só quando a fila já tem Policy (evita import em fila compartilhada sem policy)."""
    return [
        f"if sqs_policy=$(aws sqs get-queue-attributes --queue-url '{queue_url}' \\",
        f"  --attribute-names Policy --query 'Attributes.Policy' --output text 2>/dev/null) \\",
        f"  && [[ -n \"$sqs_policy\" && \"$sqs_policy\" != \"None\" ]]; then",
        f"  terraform import '{tf_addr}' '{queue_url}'",
        f"else",
        f"  echo 'Sem Queue Policy em {queue_url} — BP criará no apply'",
        f"fi",
    ]


def gen_import_commands(bucket_name, cfg, state_key, env='dev', asset_cat='Development'):
    """Gera import apenas para recursos gerenciados pela BP.
    Exclui: object_lock, intelligent_tiering, inventory, analytics.

    Endereços alinhados ao main.tf gerado: `module "s3" {{ ... }}` (BP),
    não recursos na raiz do stack.
    """
    # Nome do módulo em gen_main_tf — deve coincidir com `module "s3"`.
    mod = "module.s3"
    lc_rules = (cfg.get("lifecycle") or {}).get("Rules") or []
    has_lc_aws = bool(lc_rules)
    oc_raw = cfg.get("ownership_controls")
    has_ownership_aws = bool(
        oc_raw
        and (oc_raw.get("OwnershipControls") or {}).get("Rules")
    )
    pab_raw = cfg.get("public_access_block") or {}
    has_pab_aws = bool(pab_raw.get("PublicAccessBlockConfiguration"))

    cmds = [
        f"#!/usr/bin/env bash",
        f"# import_commands.sh — {bucket_name}",
        f"# Execute APÓS terraform init",
        f"set -euo pipefail",
        f"",
        f"echo 'Importando recursos de {bucket_name}...'",
        f"",
        f"# Recursos principais — dentro de {mod}",
        f"terraform import '{mod}.aws_s3_bucket.main' '{bucket_name}'",
        f"",
    ]
    if has_lc_aws:
        cmds += [
            f"# Lifecycle já existe na AWS",
            f"terraform import '{mod}.aws_s3_bucket_lifecycle_configuration.main' '{bucket_name}'",
            f"",
        ]
    else:
        cmds += [
            f"# Sem aws_s3_bucket_lifecycle_configuration na AWS — não importar.",
            f"# O primeiro apply da BP criará o recurso (regras padrão por asset_category/env).",
            f"",
        ]
    if has_ownership_aws:
        cmds += [
            f"terraform import '{mod}.aws_s3_bucket_ownership_controls.main' '{bucket_name}'",
            f"",
        ]
    else:
        cmds += [
            f"# Sem ownership_controls explícito na AWS — BP criará BucketOwnerEnforced no apply",
            f"",
        ]
    cmds += [
        f"terraform import '{mod}.aws_s3_bucket_logging.main' '{bucket_name}'",
        f"terraform import '{mod}.aws_s3_bucket_server_side_encryption_configuration.main' '{bucket_name}'",
    ]
    if has_pab_aws:
        cmds += [
            f"terraform import '{mod}.aws_s3_bucket_public_access_block.main[0]' '{bucket_name}'",
            f"",
        ]
    else:
        cmds += [
            f"# Sem PublicAccessBlockConfiguration na AWS — não importar; BP criará no apply",
            f"",
        ]

    ownership = get_ownership(cfg)
    if ownership != 'BucketOwnerEnforced':
        cmds += [
            f"# ACL — BP cria quando object_ownership ≠ BucketOwnerEnforced",
            f"terraform import '{mod}.aws_s3_bucket_acl.main[0]' '{bucket_name}'",
            f"",
        ]

    vers = (cfg.get('versioning') or {}).get('Status', '')
    # BP só materializa aws_s3_bucket_versioning em prd + Productive data/Embbeded
    bp_will_manage_versioning = (
        str(env).lower() == 'prd'
        and str(asset_cat).strip() in ('Productive data', 'Embbeded')
    )
    if vers in ('Enabled', 'Suspended') and bp_will_manage_versioning:
        cmds += [
            f"# Versioning (BP gerencia neste env/category)",
            f"terraform import '{mod}.aws_s3_bucket_versioning.main[0]' '{bucket_name}'",
            f"",
        ]
    elif vers in ('Enabled', 'Suspended'):
        cmds += [
            f"# Versioning {vers} na AWS — variável no main.tf, sem recurso versioning na BP neste env",
            f"# (não importar — evita falha de import e state parcial)",
            f"",
        ]

    policy = (cfg.get('bucket_policy') or {}).get('Policy', '')
    if policy:
        cmds += [
            f"# Bucket policy",
            f"terraform import '{mod}.aws_s3_bucket_policy.main[0]' '{bucket_name}'",
            f"",
        ]

    cors = (cfg.get('cors') or {}).get('CORSRules', [])
    if cors:
        cmds += [
            f"# CORS",
            f"terraform import '{mod}.aws_s3_bucket_cors_configuration.main[0]' '{bucket_name}'",
            f"",
        ]

    rep = (cfg.get('replication') or {}).get('ReplicationConfiguration', {})
    if rep:
        cmds += [
            f"# Replication (verificar dependência de versioning)",
            f"terraform import '{mod}.aws_s3_bucket_replication_configuration.main[0]' '{bucket_name}'",
            f"",
        ]

    if has_notifications(cfg):
        cmds += [
            f"# Notifications (submódulo BP — recurso usa count, endereço main[0])",
            f"terraform import '{mod}.module.notification.aws_s3_bucket_notification.main[0]' '{bucket_name}'",
        ]
        n = cfg.get('notifications') or {}
        for i, q in enumerate(n.get('QueueConfigurations', [])):
            arn = (q.get('QueueArn') or '').strip()
            if not arn:
                continue
            parts = arn.split(':')
            if len(parts) >= 6 and parts[2] == 'sqs':
                region, account, qname = parts[3], parts[4], parts[5]
                queue_url = f"https://sqs.{region}.amazonaws.com/{account}/{qname}"
                tf_addr = f'{mod}.module.notification.aws_sqs_queue_policy.allow["notification_{i}"]'
                cmds += _bash_import_sqs_queue_policy_lines(tf_addr, queue_url)
                cmds.append("")

    cmds += [
        f"echo 'Import concluído!'",
        f"echo 'Rode agora: terraform plan'",
    ]
    return '\n'.join(cmds)

def gen_changes_md(bucket_name, team, env, asset_cat, cfg,
                   log_bucket=DEFAULT_LOG_BUCKET):
    """Gera o CHANGES.md completo e detalhado para revisão da MR."""

    logical, follows_bp = extract_logical(bucket_name, team, env)
    _, tag_warnings = resolve_tags(bucket_name, team, env, asset_cat, cfg)
    enc = cfg.get('encryption') or {}
    enc_rules = enc.get('ServerSideEncryptionConfiguration', {}).get('Rules', [])
    enc_algo = enc_rules[0].get('ApplyServerSideEncryptionByDefault',{}).get('SSEAlgorithm','') if enc_rules else ''
    kms_key = get_kms_key_id(cfg)
    vers = (cfg.get('versioning') or {}).get('Status','')
    lc_rules = (cfg.get('lifecycle') or {}).get('Rules',[])
    is_bp_default_lc = (
        lifecycle_matches_bp_default(lc_rules, asset_cat, env, vers)
        if lc_rules else False
    )
    lc_mode = (
        'custom' if lc_rules and not is_bp_default_lc
        else ('bp_match' if lc_rules else 'bp_new')
    )
    lc_drift_warnings = (
        lifecycle_transition_drift_warnings(lc_rules, asset_cat, env)
        if lc_rules and lc_mode == 'bp_match'
        else []
    )
    lc_drift = bool(lc_drift_warnings)
    has_policy= bool((cfg.get('bucket_policy') or {}).get('Policy',''))
    has_cors = bool((cfg.get('cors') or {}).get('CORSRules',[]))
    has_notif = has_notifications(cfg)
    has_lambda_notif = has_lambda_notifications(cfg)
    has_rep = bool((cfg.get('replication') or {}).get('ReplicationConfiguration',{}))
    not_sup = get_not_supported(cfg)
    pub_warn = get_public_access_warnings(cfg)
    log_target, log_prefix = get_logging_info(cfg)
    ownership = get_ownership(cfg)
    oc_raw = cfg.get('ownership_controls')
    has_ownership_aws = bool(
        oc_raw and (oc_raw.get('OwnershipControls') or {}).get('Rules')
    )
    errors = cfg.get('_errors', {})

    # Risco geral derivado da severidade real — não sempre ALTO
    ns_level = _ns_max_level(not_sup)
    log_changed = bool(log_target and log_target != log_bucket)
    if has_rep:
        risco_geral = '🔴 ALTO — bucket com replication'
    elif ns_level >= 4:
        risco_geral = '🔴 ALTO — object_lock detectado (não pode ser desativado)'
    elif ns_level >= 3:
        risco_geral = '🟠 MÉDIO — config não suportada com risco ALTO'
    elif ns_level >= 2 or log_changed:
        parts = []
        if ns_level >= 2: parts.append('config não suportada')
        if log_changed: parts.append('logging alvo diferente')
        risco_geral = '🟠 MÉDIO — ' + ' + '.join(parts)
    elif enc_algo == 'AES256' or kms_key:
        extra = ' + drift lifecycle na pipeline' if lc_drift else ''
        risco_geral = f'🟠 MÉDIO — encryption customizada{extra}'
    elif lc_drift:
        risco_geral = '🟠 MÉDIO — drift lifecycle na pipeline (IDs BP, transições AWS)'
    elif lc_mode == 'custom':
        risco_geral = '🟡 MÉDIO — lifecycle customizado preservado no main.tf (revisor do projeto)'
    elif lc_mode == 'bp_match':
        risco_geral = '🟡 BAIXO — lifecycle na AWS alinhado ao padrão BP'
    else:
        risco_geral = '🟠 MÉDIO — lifecycle será criado pela BP no apply (plan: +1 lifecycle)'

    lines = [
        f"# 📦 Import S3 — `{bucket_name}`",
        f"",
        f"> **Time:** `{team}` | **Env:** `{env}` | **Asset Category:** `{asset_cat}` ",
        f"> **Operação:** Import de bucket existente → Blueprint S3 ",
        f"> **Risco geral:** {risco_geral}",
    ]

    if not follows_bp:
        lines += [
            f"",
            f"> ⚡ **Nome fora do padrão BP** — `tag_legacy_name = \"{bucket_name}\"` garante que o nome não será alterado.",
        ]

    if errors:
        lines += [
            f"",
            f"> ⚠️ **Erros na extração de config:** `{', '.join(errors.keys())}` — verificar manualmente.",
        ]

    plan_adds, plan_changes, plan_summary = estimate_plan_expectations(
        cfg, lc_mode, lc_drift_warnings, has_ownership_aws
    )
    module_files = 'backend.tf, main.tf, outputs.tf, versions.tf'
    if has_policy:
        module_files += ', files/policy.json (sem policy.json na raiz)'

    lines += [
        f"",
        f"---",
        f"",
        f"## 📊 Plan na pipeline (heurística pós-import)",
        f"",
        f"{plan_summary}",
        f"",
        f"| Ação | Recurso | Notas |",
        f"|---|---|---|",
    ]
    for action, resource, note in plan_adds + plan_changes:
        lines.append(f"| {action} | `{resource}` | {note} |")
    lines += [
        f"",
        f"**Arquivos do módulo:** `{module_files}`.",
        f"",
        f"> Valores exatos (`1 add, 4 change`, etc.) vêm da pipeline — esta tabela antecipa o perfil típico do import BP.",
    ]

    # ── Lifecycle — bloco prioritário para o revisor do projeto ──────────────
    lines += [f"", f"---", f"", f"## 🧭 Lifecycle — leia antes do merge", f""]
    if lc_mode == 'bp_new':
        lines += [
            f"**Modo desta MR:** ℹ️ **BP padrão** — não há lifecycle na AWS hoje.",
            f"",
            f"| Item | Detalhe |",
            f"|---|---|",
            f"| `main.tf` | **Sem** bloco `lifecycle_rules` — a Blueprint aplica regras padrão para `{asset_cat}` / `{env}` |",
            f"| Plan na pipeline | Espere **`+ create`** em `aws_s3_bucket_lifecycle_configuration` — **não é criação de bucket** |",
            f"| Apply | Cria o recurso de lifecycle no Terraform com regras padrão da BP |",
            f"| Revisor do projeto | Confirmar que as regras padrão BP para este asset/env são aceitáveis para o serviço |",
            f"",
        ]
    elif lc_mode == 'custom':
        lines += [
            f"**Modo desta MR:** ⚠️ **Lifecycle preservado** — regras extraídas da AWS estão no código.",
            f"",
            f"| Item | Detalhe |",
            f"|---|---|",
            f"| `main.tf` | Bloco **`lifecycle_rules`** com {len(lc_rules)} regra(s) — **validar regra a regra** |",
            f"| Plan na pipeline | Após import: **`0 to add`** em lifecycle; só `~ change` (tags, filters, etc.) |",
            f"| Revisor do projeto | **Obrigatório:** comparar IDs/regras abaixo com o que o negócio espera; se o plan remover `transition` ou regra, **não mergear** sem alinhar |",
            f"",
        ]
    elif lc_drift: # bp_match com transições que o plan costuma remover
        lines += [
            f"**Modo desta MR:** 🟡 **Padrão BP com drift na pipeline** — IDs BP; `main.tf` sem `lifecycle_rules`.",
            f"",
            f"| Item | Detalhe |",
            f"|---|---|",
            f"| `main.tf` | Sem `lifecycle_rules` — BP aplica padrão `{asset_cat}`/`{env}` |",
            f"| Plan lifecycle | **`~ update`** — ver tabela de plan acima |",
            f"| Decisão de merge | Time **{team}** aceita remoção de transição(ões) Glacier **ou** ajusta `lifecycle_rules` no `main.tf` |",
            f"",
        ]
    else: # bp_match sem drift conhecido
        lines += [
            f"**Modo desta MR:** ✅ **Alinhado à BP** — lifecycle na AWS coincide com o padrão da Blueprint.",
            f"",
            f"| Item | Detalhe |",
            f"|---|---|",
            f"| `main.tf` | Sem `lifecycle_rules` explícito — BP gerencia automaticamente |",
            f"| Plan na pipeline | Após import: conferir tabela acima; lifecycle sem `+ create` típico |",
            f"| Revisor do projeto | Validar drift menor (filters/tags); regras padrão já existem na AWS |",
            f"",
        ]

    if lc_mode == 'bp_new':
        apply_intro = (
            f"> ⚠️ O plan **pode mostrar `+ create`** em lifecycle — esperado. \n"
            f"> Demais linhas: tags, logging, etc. — ver tabela abaixo."
        )
    else:
        add_hint = ''
        if has_lambda_notif or (not has_ownership_aws):
            parts = []
            if has_lambda_notif:
                parts.append('`+1` lambda_permission')
            if not has_ownership_aws:
                parts.append('ownership')
            add_hint = f"**{', '.join(parts)}** e "
        apply_intro = (
            f"> **0 destroy** — bucket preservado via `tag_legacy_name`. \n"
            f"> {add_hint}**updates** típicos: tags, logging, notification — ver plan na pipeline."
        )

    lines += [
        f"", f"---", f"", f"## ⚠️ O que muda com o `terraform apply`", f"",
        apply_intro,
        f"",
        f"| Recurso AWS | Status | Detalhe | Risco |",
        f"|---|---|---|:---:|",
        f"| `aws_s3_bucket` | 🟡 TAGS | Nome `{bucket_name}` via `tag_legacy_name`; tags normalizadas pela BP | 🟡 |",
    ]

    # Encryption
    if enc_algo == 'AES256':
        lines.append(
            f"| `aws_s3_bucket_server_side_encryption_configuration` | ✅ SEM ALTERAÇÃO | "
            f"`AES256` explícito no main.tf — **NÃO será trocado para KMS** | ✅ |")
    elif kms_key:
        lines.append(
            f"| `aws_s3_bucket_server_side_encryption_configuration` | ✅ SEM ALTERAÇÃO | "
            f"`aws:kms` com CMK customizada preservada (`{kms_key[:30]}...`) | ✅ |")
    else:
        lines.append(
            f"| `aws_s3_bucket_server_side_encryption_configuration` | ✅ SEM ALTERAÇÃO | "
            f"`aws:kms` padrão da BP | ✅ |")

    # Lifecycle (tabela resumo)
    if lc_mode == 'custom':
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | 🟡 PRESERVADO NO CÓDIGO | "
            f"{len(lc_rules)} regra(s) em `lifecycle_rules` no `main.tf` — **revisor do projeto** | 🟡 |")
    elif lc_mode == 'bp_match' and lc_drift:
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | 🟡 DRIFT | "
            f"{len(lc_rules)} regra(s) — plan pode remover transição Glacier (ver seção lifecycle) | 🟠 |")
    elif lc_mode == 'bp_match':
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | ✅ PADRÃO BP | "
            f"{len(lc_rules)} regra(s) na AWS = padrão BP — importado, sem bloco no main.tf | ✅ |")
    else:
        lines.append(
            f"| `aws_s3_bucket_lifecycle_configuration` | 🟠 CRIADO NO APPLY | "
            f"Sem LC na AWS — BP cria padrão `{asset_cat}`/`{env}` (**`+ create` no plan**) | 🟠 |")

    # Versioning
    if vers in ('Enabled','Suspended'):
        lines.append(
            f"| `aws_s3_bucket_versioning` | ✅ SEM ALTERAÇÃO | "
            f"Status `{vers}` mantido | ✅ |")

    # Policy
    if has_policy:
        lines.append(
            f"| `aws_s3_bucket_policy` | ✅ PRESERVADO | "
            f"`policy_json = file(\"${{path.module}}/files/policy.json\")` | ✅ |")
    else:
        lines.append(
            f"| `aws_s3_bucket_policy` | ✅ SEM ALTERAÇÃO | "
            f"Sem policy configurada | ✅ |")

    # CORS
    if has_cors:
        cors_count = len((cfg.get('cors') or {}).get('CORSRules',[]))
        lines.append(
            f"| `aws_s3_bucket_cors_configuration` | ✅ PRESERVADO | "
            f"{cors_count} regra(s) CORS extraída(s) e incluída(s) no main.tf | ✅ |")

    # Notifications
    if has_lambda_notif:
        lines.append(
            f"| `aws_lambda_permission` | 🟢 **+ CREATE** | "
            f"BP gerencia invoke S3→Lambda (`allow[...]` no submódulo notification) | 🟢 |")
    if has_notif:
        n = cfg.get('notifications') or {}
        types = []
        if n.get('QueueConfigurations'): types.append(f"{len(n['QueueConfigurations'])} SQS")
        if n.get('TopicConfigurations'): types.append(f"{len(n['TopicConfigurations'])} SNS")
        if n.get('LambdaFunctionConfigurations'): types.append(f"{len(n['LambdaFunctionConfigurations'])} Lambda")
        if n.get('EventBridgeConfiguration'):types.append("EventBridge")
        notif_status = '🟡 ID TF' if has_lambda_notif else '✅ PRESERVADO'
        notif_detail = (
            f"{', '.join(types)} — Lambda no main.tf; plan pode renomear chave no state"
            if has_lambda_notif
            else f"{', '.join(types)} — extraído e incluído no main.tf"
        )
        lines.append(
            f"| `aws_s3_bucket_notification` | {notif_status} | "
            f"{notif_detail} | {'🟡' if has_lambda_notif else '✅'} |")

    # Replication
    if has_rep:
        lines.append(
            f"| `aws_s3_bucket_replication_configuration` | 🔴 VERIFICAR | "
            f"Replication configurada — verificar bucket destino antes do apply | 🔴 |")

    # Logging
    bp_log_prefix = f"{bucket_name}/"
    if log_target and log_target != log_bucket:
        lines.append(
            f"| `aws_s3_bucket_logging` | 🟠 ALTERAÇÃO | "
            f"Destino atual: `{log_target}` → BP vai usar: `{log_bucket}` | 🟠 |")
    else:
        prefix_note = ""
        if log_prefix and log_prefix != bp_log_prefix:
            prefix_note = (f" Prefix atual: `{log_prefix}` → BP vai usar `{bp_log_prefix}` "
                           f"(partitioned). **Queries Athena/CloudWatch que usam o prefix antigo "
                           f"precisarão ser atualizadas.**")
        lines.append(
            f"| `aws_s3_bucket_logging` | 🟡 UPDATE | "
            f"BP gerencia logging → `{log_bucket}`; plan típico adiciona prefixo particionado.{prefix_note} | 🟡 |")

    # Ownership
    if not has_ownership_aws:
        lines.append(
            f"| `aws_s3_bucket_ownership_controls` | 🟠 CRIADO NO APPLY | "
            f"Sem recurso na AWS — BP define `BucketOwnerEnforced` (**pode aparecer `+ create` no plan**) | 🟠 |")
    elif ownership != 'BucketOwnerEnforced':
        lines.append(
            f"| `aws_s3_bucket_ownership_controls` | 🟠 DIFERENTE DO PADRÃO | "
            f"Atual: `{ownership}` — mantido explicitamente no main.tf | 🟠 |")
    else:
        lines.append(
            f"| `aws_s3_bucket_ownership_controls` | ✅ SEM ALTERAÇÃO | "
            f"`BucketOwnerEnforced` — importado da AWS | ✅ |")

    # Public access block warnings
    if pub_warn:
        for w in pub_warn:
            lines.append(
                f"| `aws_s3_bucket_public_access_block` | 🟠 MUDANÇA | {w} | 🟠 |")

    # Configs não suportadas — distingue mecanismo por tipo
    if not_sup:
        no_import_items = [(ns, r) for ns, r in not_sup if ns in _SEPARATE_NO_IMPORT_CONFIGS]
        attr_ignore_items = [(ns, r) for ns, r in not_sup if ns in _BUCKET_ATTR_IGNORE_CONFIGS]
        lines += [f"", f"---", f"", f"## ⚠️ Configurações fora do escopo da Blueprint", f""]
        if no_import_items:
            lines += [
                f"### Recursos não importados — fora do state",
                f"> A Blueprint **não gerencia** estes recursos. Eles **não são importados**",
                f"> para o Terraform state. O plan **não vai propor destroy** — são invisíveis.",
                f"",
            ]
            for ns_config, ns_risk in no_import_items:
                lines += [
                    f"- **`{ns_config}`** — {ns_risk}",
                    f"  → Gerenciar fora da Blueprint se necessário",
                    f"",
                ]
        if attr_ignore_items:
            lines += [
                f"### Atributos com `ignore_changes` em `aws_s3_bucket.main`",
                f"> Estes atributos existem no bucket. O Terraform **não vai removê-los**",
                f"> (protegidos via `lifecycle {{ ignore_changes }}`). Gerenciar manualmente.",
                f"",
            ]
            for ns_config, ns_risk in attr_ignore_items:
                lines += [
                    f"- **`{ns_config}`** — {ns_risk}",
                    f"",
                ]

    # Encryption AES256 em destaque
    if enc_algo == 'AES256':
        lines += [
            f"---", f"",
            f"## 🔒 Encryption — Confirmação obrigatória",
            f"",
            f"```",
            f"╔══════════════════════════════════════════════════════════╗",
            f"║ ⛔ BUCKET COM AES256 ║",
            f"║ A Blueprint usa aws:kms por padrão. ║",
            f"║ sse_algorithm = \"AES256\" está explícito no main.tf. ║",
            f"║ ✅ NÃO SERÁ ALTERADO. ║",
            f"╚══════════════════════════════════════════════════════════╝",
            f"```",
            f"",
            f"> **No plan:** se aparecer `-/+ replace` em encryption → **NÃO APROVAR**, acionar SRE.",
        ]

    # Lifecycle detalhado — IDs para o revisor do projeto
    if lc_rules:
        lines += [
            f"", f"---", f"",
            f"## 🔄 Regras de lifecycle ({len(lc_rules)})",
            f"",
        ]
        if lc_mode == 'custom':
            lines.append(
                f"> Código: ver bloco `lifecycle_rules` em `{logical}/dev/main.tf` neste branch."
            )
        elif lc_drift:
            lines.append(
                f"> Regras na AWS; plan típico remove transição Glacier na regra abaixo (ver checklist):"
            )
        else:
            lines.append(f"> Regras atuais na AWS (padrão BP — não repetidas no main.tf):")
        lines.append(f"")
        for rule in lc_rules:
            rid = rule.get('ID', rule.get('id', '?'))
            st = rule.get('Status', '?')
            extra = []
            exp = rule.get('Expiration') or {}
            if exp.get('Days') is not None:
                extra.append(f"expire {exp['Days']}d")
            if exp.get('ExpiredObjectDeleteMarker'):
                extra.append("delete marker")
            for t in rule.get('Transitions') or []:
                extra.append(f"{t.get('Days')}d→{t.get('StorageClass')}")
            suffix = f" — {', '.join(extra)}" if extra else ""
            lines.append(f"- **`{rid}`** [{st}]{suffix}")
    elif lc_mode == 'bp_new':
        bp_ids = sorted(bp_default_rule_ids(asset_cat, env, vers))
        lines += [
            f"", f"---", f"",
            f"## 🔄 Lifecycle — regras que a BP criará no apply",
            f"",
            f"> Não há lifecycle na AWS. Após apply, espera-se regras com IDs próximos a:",
            f"",
        ]
        for rid in bp_ids:
            lines.append(f"- **`{rid}`** (padrão BP)")

    # Aviso SNS — nunca testado end-to-end na BP (antes do rodapé!)
    if (cfg.get('notifications') or {}).get('TopicConfigurations'):
        lines += [
            f"", f"---", f"",
            f"## 🚨 SNS Notifications — Validação Obrigatória",
            f"",
            f"> ⚠️ SNS notifications configuradas. Suporte mapeado na Blueprint mas",
            f"> **nunca testado em produção end-to-end**.",
            f"> Verificar no plan se há `destroy` ou `replace` na notification. Se sim, **NÃO APROVAR** — acionar Cross SRE.",
        ]

    # Checklist — só itens que exigem decisão real do revisor
    critical_items = []
    if enc_algo == 'AES256':
        critical_items.append(f"**Encryption AES256:** plan NÃO deve propor troca para `aws:kms` — se propuser, **NÃO APROVAR**")
    if kms_key:
        critical_items.append(f"**KMS CMK:** chave `{kms_key[:30]}...` — plan NÃO deve propor troca de chave")
    if has_rep:
        critical_items.append(f"**Replication 🔴:** verificar bucket destino e role ARN antes de aprovar")
    if log_target and log_target != log_bucket:
        critical_items.append(f"**Logging alterado:** `{log_target}` → `{log_bucket}` — confirmar que o destino novo está correto")
    if log_prefix and log_prefix != bp_log_prefix:
        critical_items.append(
            f"**Logging prefix alterado:** `{log_prefix}` → `{bp_log_prefix}` — "
            f"confirmar que queries Athena/CloudWatch/S3 Select que filtram por esse "
            f"prefix foram atualizadas"
        )
    for ns_config, _ in not_sup:
        if ns_config in _BUCKET_ATTR_IGNORE_CONFIGS:
            critical_items.append(f"**{ns_config}:** gerenciado via `ignore_changes` — verificar se o plan não propõe remover")
    if pub_warn:
        critical_items.append(f"**Public access 🔴:** BP vai ativar `block_public_*` — confirmar que o bucket não precisa ser público")
    if tag_warnings:
        for w in tag_warnings:
            critical_items.append(f"**Tag:** {w}")

    if lc_mode == 'bp_new':
        critical_items.insert(
            0,
            "**Lifecycle BP (novo):** plan com **`+ create`** só em `lifecycle_configuration` — "
            "confirmar regras padrão aceitáveis para o serviço",
        )
    elif lc_mode == 'custom':
        critical_items.insert(
            0,
            "**Lifecycle preservado:** plan **`0 to add`** em lifecycle; validar cada ID em "
            "`lifecycle_rules` vs operação do bucket",
        )
    elif lc_drift:
        critical_items.insert(
            0,
            "**Lifecycle drift:** aceitar remoção de transição Glacier no plan **ou** "
            "bloquear merge até alinhar `lifecycle_rules` no `main.tf`",
        )
    elif lc_rules:
        critical_items.insert(
            0,
            "**Lifecycle padrão BP:** conferir tabela de plan; validar transitions/filters na pipeline",
        )
    if has_lambda_notif:
        critical_items.append(
            "**+1 lambda_permission:** esperado para notification Lambda existente"
        )
    if has_policy:
        critical_items.append(
            "**Policy:** apenas `files/policy.json` (sem duplicata na raiz do módulo)"
        )

    lines += [f"", f"---", f"", f"## ✅ Checklist do revisor", f""]
    lines.append(f"- [ ] **Plan:** `0 destroy` — confirmar na pipeline; comparar com tabela heurística acima")
    if lc_mode == 'bp_new':
        lines.append(
            f"- [ ] **Lifecycle +create:** aceito que a BP cria LC neste apply (não é bucket novo)"
        )
    elif lc_mode == 'custom':
        lines.append(f"- [ ] **Lifecycle no código:** revisei `lifecycle_rules` no `main.tf`")
    for item in critical_items:
        lines.append(f"- [ ] {item}")
    if not critical_items:
        lines.append(f"- [ ] Revisar diff do `main.tf` — nenhum item crítico detectado automaticamente")

    lines += [
        f"",
        f"---",
        f"*Gerado por s3_main_tf_gen.py — {datetime.now().strftime('%Y-%m-%d')}*",
    ]

    return '\n'.join(lines)



# ── Arquivos adicionais com estrutura real ─────────────────────────────────────

def gen_versions_tf():
    """versions.tf — sem required_version, sem versão do provider (padrão ECS)."""
    return """terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}
"""

def gen_outputs_tf():
    """outputs.tf — outputs padrão da Blueprint S3."""
    return """output "bucket_arn" {
  description = "ARN do bucket S3"
  value = module.s3.arn
}

output "bucket_id" {
  description = "Nome/ID do bucket S3"
  value = module.s3.id
}

output "bucket_region" {
  description = "Região do bucket S3"
  value = module.s3.region
}

output "bucket_domain_name" {
  description = "Domain name do bucket S3"
  value = module.s3.bucket_domain_name
}

output "bucket_regional_domain_name" {
  description = "Regional domain name (recomendado para CloudFront)"
  value = module.s3.bucket_regional_domain_name
}
"""

def gen_backend_tf(bucket_name, team, env, state_bucket, state_region):
    """
    backend.tf — provider aws + backend s3 no mesmo arquivo (padrão ECS real).
    """
    logical, _ = extract_logical(bucket_name, team, env)
    repo_name = f"ecs-{team}-default-aws-terraform"
    key = f"{repo_name}/services/s3/{logical}/{env}/terraform.tfstate"

    return f"""provider "aws" {{
  region = "{state_region}"
}}

terraform {{
  backend "s3" {{
    bucket = "{state_bucket}"
    key = "{key}"
    region = "{state_region}"
  }}
}}
"""

def gen_policy_file(cfg):
    """
    Retorna o conteúdo do arquivo files/policy.json se o bucket tiver policy.
    Retorna None se não tiver policy.
    """
    policy_raw = (cfg.get('bucket_policy') or {}).get('Policy', '')
    if not policy_raw:
        return None
    try:
        # Parseia e reformata com indentação legível
        policy_obj = json.loads(policy_raw)
        return json.dumps(policy_obj, indent=2, ensure_ascii=False)
    except Exception:
        return policy_raw

def gen_all_files(bucket_name, team, env, asset_cat, cfg,
                  state_bucket, state_region, log_bucket=DEFAULT_LOG_BUCKET, ticket="PREENCHER"):
    """
    Gera todos os arquivos necessários para uma pasta de bucket.
    Retorna dict: {filename: content}
    Inclui files/policy.json se o bucket tiver policy.
    """
    asset_cat = normalize_asset_category(asset_cat)
    files = {}

    # versions.tf
    files['versions.tf'] = gen_versions_tf()

    # outputs.tf
    files['outputs.tf'] = gen_outputs_tf()

    # backend.tf
    files['backend.tf'] = gen_backend_tf(
        bucket_name, team, env, state_bucket, state_region
    )

    # main.tf — sem o bloco backend (fica no backend.tf) e sem policy inline
    files['main.tf'] = gen_main_tf(
        bucket_name, team, env, asset_cat, cfg,
        state_bucket, state_region, log_bucket, ticket
    )

    # files/policy.json — só se tiver policy
    policy_content = gen_policy_file(cfg)
    if policy_content:
        files['files/policy.json'] = policy_content

    # import_commands.sh — salvo separado (não vai pro repo)
    logical, _ = extract_logical(bucket_name, team, env)
    repo_name = f"ecs-{team}-default-aws-terraform"
    state_key = f"{repo_name}/services/s3/{logical}/{env}/terraform.tfstate"
    # Guardado em _import_commands para uso local, não incluído nos arquivos do repo
    files['_import_commands.sh'] = gen_import_commands(
        bucket_name, cfg, state_key, env=env, asset_cat=asset_cat
    )

    # CHANGES.md
    files['CHANGES.md'] = gen_changes_md(
        bucket_name, team, env, asset_cat, cfg, log_bucket
    )

    return files

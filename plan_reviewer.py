#!/usr/bin/env python3
"""
plan_reviewer.py — Analisa terraform plan -json e classifica cada mudança por severidade.

Lê os arquivos plan_output.json gerados pela fase3 do S3_migrate.py e produz um relatório
Markdown com before/after detalhado de lifecycle, policy e outros recursos S3.

Uso:
  python3 plan_reviewer.py --plan-dir mr_output/            # todos os buckets
  python3 plan_reviewer.py --bucket ecs-ecred-consumer-dev  # um bucket
  python3 plan_reviewer.py --plan-json plan_output.json     # arquivo direto
  python3 plan_reviewer.py --plan-dir mr_output/ --output review.md --fail-on-blocked
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# ── Severidades ───────────────────────────────────────────────────────────────

BLOCKED = 'BLOCKED'  # não aprovar sem resolver
REVIEW  = 'REVIEW'   # revisor humano deve confirmar
INFO    = 'INFO'     # informativo, sem ação necessária
SAFE    = 'SAFE'     # nenhuma mudança relevante detectada

_VERDICT_ORDER = {BLOCKED: 0, REVIEW: 1, INFO: 2, SAFE: 3}

# Recursos cujo destroy deve sempre bloquear a MR
_NEVER_DESTROY = {
    'aws_s3_bucket',
    'aws_s3_bucket_policy',
    'aws_s3_bucket_replication_configuration',
    'aws_s3_bucket_server_side_encryption_configuration',
    'aws_s3_bucket_lifecycle_configuration',
    'aws_s3_bucket_versioning',
}

# Recursos que podem ser destruídos/recriados normalmente (rename de key, BP recreates, etc.)
_SAFE_DESTROY_SUBSTRINGS = [
    'aws_sqs_queue_policy',
    'aws_s3_bucket_acl',
    'time_static',
    'time_rotating',
    'random_',
    'aws_s3_bucket_metric',
]

# ── Análise principal ─────────────────────────────────────────────────────────

def review_plan(plan_json: dict, bucket_name: str) -> dict:
    """Analisa um plan JSON e retorna resultado com findings classificados."""
    findings: list[dict] = []
    verdict = SAFE

    for rc in plan_json.get('resource_changes', []):
        address = rc.get('address', '')
        rtype   = rc.get('type', '')
        change  = rc.get('change', {})
        actions = change.get('actions', [])
        before  = change.get('before') or {}
        after   = change.get('after') or {}

        if actions == ['no-op'] or not actions:
            continue

        # ── Destroy ──────────────────────────────────────────────────────────
        if 'delete' in actions:
            is_safe = any(s in address for s in _SAFE_DESTROY_SUBSTRINGS)
            sev = INFO if is_safe else BLOCKED
            findings.append({
                'severity': sev,
                'resource': address,
                'action':   'destroy',
                'message':  ('Destroy esperado (renaming/BP recria)' if is_safe
                             else f'Destroy inesperado — revisar antes de aprovar'),
            })
            verdict = _worst(verdict, sev)
            continue

        # ── Lifecycle ─────────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_lifecycle_configuration':
            f = _analyze_lifecycle(address, actions, before, after)
            if f:
                findings.append(f)
                verdict = _worst(verdict, f['severity'])
            continue

        # ── Bucket policy ─────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_policy':
            findings.append({
                'severity': REVIEW,
                'resource': address,
                'action':   '+'.join(actions),
                'message':  'Bucket policy alterada — conferir statements no plan',
                'details':  _policy_diff(before.get('policy'), after.get('policy')),
            })
            verdict = _worst(verdict, REVIEW)
            continue

        # ── Encryption ────────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_server_side_encryption_configuration':
            b_algo = _enc_algo(before)
            a_algo = _enc_algo(after)
            msg = (f'Encryption: `{b_algo}` → `{a_algo}` — confirmar que algoritmo não muda'
                   if b_algo != a_algo else 'Encryption atualizada (algoritmo mantido)')
            sev = BLOCKED if (b_algo and a_algo and b_algo != a_algo) else REVIEW
            findings.append({'severity': sev, 'resource': address,
                             'action': '+'.join(actions), 'message': msg})
            verdict = _worst(verdict, sev)
            continue

        # ── Versioning ────────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_versioning':
            b_st = _versioning_status(before)
            a_st = _versioning_status(after)
            if b_st != a_st:
                findings.append({
                    'severity': REVIEW,
                    'resource': address,
                    'action':   '+'.join(actions),
                    'message':  f'Versioning: `{b_st}` → `{a_st}`',
                })
                verdict = _worst(verdict, REVIEW)
            continue

        # ── Replication ───────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_replication_configuration':
            findings.append({
                'severity': REVIEW,
                'resource': address,
                'action':   '+'.join(actions),
                'message':  'Replication alterada — verificar bucket destino, role ARN e permissões',
            })
            verdict = _worst(verdict, REVIEW)
            continue

        # ── Notifications ─────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_notification':
            findings.append({
                'severity': INFO,
                'resource': address,
                'action':   '+'.join(actions),
                'message':  'Notificações S3 atualizadas — verificar se endpoints SQS/SNS/Lambda estão corretos',
            })
            continue

        # ── Logging ───────────────────────────────────────────────────────────
        if rtype == 'aws_s3_bucket_logging':
            b_prefix = (before.get('target_prefix') or '')
            a_prefix = (after.get('target_prefix') or '')
            if b_prefix != a_prefix:
                findings.append({
                    'severity': REVIEW,
                    'resource': address,
                    'action':   '+'.join(actions),
                    'message':  (f'Logging prefix alterado: `{b_prefix or "(vazio)"}` → `{a_prefix}` — '
                                 f'confirmar que queries Athena/CloudWatch foram atualizadas'),
                })
                verdict = _worst(verdict, REVIEW)
            else:
                findings.append({
                    'severity': INFO,
                    'resource': address,
                    'action':   '+'.join(actions),
                    'message':  'Logging atualizado (prefix mantido)',
                })
            continue

        # ── Bucket principal (tags, ownership, PAB) ───────────────────────────
        if rtype == 'aws_s3_bucket':
            if _is_tag_only(before, after):
                findings.append({
                    'severity': INFO,
                    'resource': address,
                    'action':   'update',
                    'message':  'Apenas tags atualizadas (sem impacto operacional)',
                })
            else:
                findings.append({
                    'severity': INFO,
                    'resource': address,
                    'action':   '+'.join(actions),
                    'message':  'aws_s3_bucket atualizado (tags + metadados BP)',
                })
            continue

        # ── Qualquer outro create/update ───────────────────────────────────────
        if actions == ['create']:
            findings.append({
                'severity': INFO,
                'resource': address,
                'action':   'create',
                'message':  f'Recurso criado pela BP (esperado no import)',
            })
        elif 'update' in actions:
            findings.append({
                'severity': INFO,
                'resource': address,
                'action':   '+'.join(actions),
                'message':  f'Recurso atualizado — verificar se esperado',
            })

    if not findings:
        findings.append({
            'severity': INFO,
            'resource': '-',
            'action':   'no-op',
            'message':  'Sem mudanças detectadas — bucket já está correto',
        })
        verdict = SAFE

    return {
        'bucket':   bucket_name,
        'verdict':  verdict,
        'findings': findings,
    }


# ── Helpers de análise ────────────────────────────────────────────────────────

def _worst(current: str, candidate: str) -> str:
    return current if _VERDICT_ORDER[current] <= _VERDICT_ORDER[candidate] else candidate


def _analyze_lifecycle(address: str, actions: list, before: dict, after: dict) -> dict | None:
    b_rules = {_rule_id(r): r for r in (before.get('rule') or [])}
    a_rules = {_rule_id(r): r for r in (after.get('rule') or [])}

    removed = set(b_rules) - set(a_rules)
    added   = set(a_rules) - set(b_rules)
    changed = {k for k in b_rules.keys() & a_rules.keys() if b_rules[k] != a_rules[k]}

    if not (removed or added or changed):
        return None

    details: list[str] = []
    severity = INFO

    for rid in sorted(removed):
        details.append(f"- ❌ **REMOVIDA:** regra `{rid}` [{b_rules[rid].get('status','?')}]")
        details.extend(_rule_summary(b_rules[rid], prefix='  '))
        severity = BLOCKED  # remover regra existente bloqueia sempre

    for rid in sorted(added):
        details.append(f"- ➕ **ADICIONADA:** regra `{rid}` [{a_rules[rid].get('status','?')}]")
        details.extend(_rule_summary(a_rules[rid], prefix='  '))
        severity = _worst(severity, REVIEW)

    for rid in sorted(changed):
        details.append(f"- 📝 **ALTERADA:** regra `{rid}`")
        details.extend(_lifecycle_rule_diff(b_rules[rid], a_rules[rid]))
        severity = _worst(severity, REVIEW)

    return {
        'severity': severity,
        'resource': address,
        'action':   '+'.join(actions),
        'message':  (f'Lifecycle: {len(added)} add, {len(changed)} change, {len(removed)} remove'
                     + (' — ❌ REGRA REMOVIDA' if removed else '')),
        'details':  details,
    }


def _rule_id(rule: dict) -> str:
    return rule.get('id') or rule.get('prefix') or '(sem ID)'


def _rule_summary(rule: dict, prefix: str = '') -> list[str]:
    lines = []
    exp = (rule.get('expiration') or [{}])[0] if rule.get('expiration') else {}
    if exp.get('days'):
        lines.append(f"{prefix}expire: {exp['days']}d")
    if exp.get('expired_object_delete_marker'):
        lines.append(f"{prefix}expired_object_delete_marker: true")
    for t in (rule.get('transition') or []):
        lines.append(f"{prefix}transition: {t.get('days')}d → {t.get('storage_class')}")
    nce = (rule.get('noncurrent_version_expiration') or [{}])[0] if rule.get('noncurrent_version_expiration') else {}
    if nce.get('noncurrent_days'):
        lines.append(f"{prefix}noncurrent_expire: {nce['noncurrent_days']}d "
                     f"(keep {nce.get('newer_noncurrent_versions','todos')})")
    return lines


def _lifecycle_rule_diff(before: dict, after: dict) -> list[str]:
    lines: list[str] = []

    def _exp(r):
        return (r.get('expiration') or [{}])[0] if r.get('expiration') else {}

    b_exp, a_exp = _exp(before), _exp(after)
    if b_exp.get('days') != a_exp.get('days'):
        lines.append(f"  expiration.days: `{b_exp.get('days')}` → `{a_exp.get('days')}`")
    if b_exp.get('expired_object_delete_marker') != a_exp.get('expired_object_delete_marker'):
        lines.append(f"  expired_object_delete_marker: "
                     f"`{b_exp.get('expired_object_delete_marker')}` → "
                     f"`{a_exp.get('expired_object_delete_marker')}`")

    b_trans = {(t.get('storage_class', '?'), t.get('days', 0))
               for t in (before.get('transition') or [])}
    a_trans = {(t.get('storage_class', '?'), t.get('days', 0))
               for t in (after.get('transition') or [])}
    for sc, d in sorted(b_trans - a_trans):
        lines.append(f"  ❌ transition removida: {d}d → {sc}")
    for sc, d in sorted(a_trans - b_trans):
        lines.append(f"  ➕ transition adicionada: {d}d → {sc}")

    if before.get('status') != after.get('status'):
        lines.append(f"  status: `{before.get('status')}` → `{after.get('status')}`")

    def _nce(r):
        return (r.get('noncurrent_version_expiration') or [{}])[0] if r.get('noncurrent_version_expiration') else {}
    b_nce, a_nce = _nce(before), _nce(after)
    if b_nce.get('noncurrent_days') != a_nce.get('noncurrent_days'):
        lines.append(f"  noncurrent_expire.days: "
                     f"`{b_nce.get('noncurrent_days')}` → `{a_nce.get('noncurrent_days')}`")

    if not lines:
        lines.append('  (diferença em campos internos do Terraform state — sem impacto real)')
    return lines


def _policy_diff(before_raw: str | None, after_raw: str | None) -> list[str]:
    lines: list[str] = []
    try:
        b = json.loads(before_raw or '{}')
        a = json.loads(after_raw or '{}')
        b_stmts = {s.get('Sid', str(i)): s for i, s in enumerate(b.get('Statement', []))}
        a_stmts = {s.get('Sid', str(i)): s for i, s in enumerate(a.get('Statement', []))}
        for sid in set(b_stmts) - set(a_stmts):
            lines.append(f"  ❌ Statement removido: `{sid}`")
        for sid in set(a_stmts) - set(b_stmts):
            lines.append(f"  ➕ Statement adicionado: `{sid}`")
        for sid in set(b_stmts) & set(a_stmts):
            if b_stmts[sid] != a_stmts[sid]:
                lines.append(f"  📝 Statement alterado: `{sid}`")
    except Exception:
        if before_raw != after_raw:
            lines.append('  (policy alterada — comparar manualmente)')
    return lines


def _enc_algo(cfg: dict) -> str:
    rules = (cfg.get('rule') or [{}])
    sse = (rules[0] if rules else {}).get('apply_server_side_encryption_by_default') or {}
    return sse.get('sse_algorithm', '')


def _versioning_status(cfg: dict) -> str:
    vc = cfg.get('versioning_configuration') or [{}]
    return (vc[0] if vc else {}).get('status', '?')


def _is_tag_only(before: dict, after: dict) -> bool:
    skip = {'tags', 'tags_all', 'id'}
    b = {k: v for k, v in before.items() if k not in skip}
    a = {k: v for k, v in after.items() if k not in skip}
    return b == a


# ── Formatação do relatório ───────────────────────────────────────────────────

def format_report(results: list[dict]) -> str:
    blocked = [r for r in results if r['verdict'] == BLOCKED]
    review  = [r for r in results if r['verdict'] == REVIEW]
    info    = [r for r in results if r['verdict'] == INFO]
    safe    = [r for r in results if r['verdict'] == SAFE]

    lines = [
        "## 🔍 Plan Review — S3 Migration",
        "",
        f"| Resultado | Qtd |",
        f"|---|---|",
        f"| 🔴 Bloqueados (não aprovar) | {len(blocked)} |",
        f"| 🟡 Revisar antes de aprovar | {len(review)} |",
        f"| 🔵 Informativos | {len(info)} |",
        f"| ✅ Sem mudanças relevantes | {len(safe)} |",
        "",
    ]

    if blocked:
        lines += ["---", "", "### 🔴 Bloqueados — NÃO APROVAR sem resolução", ""]
        for r in blocked:
            lines.append(f"#### `{r['bucket']}`")
            for f in r['findings']:
                if f['severity'] == BLOCKED:
                    lines.append(f"- ❌ **{f['action'].upper()}** — `{f['resource']}`")
                    lines.append(f"  > {f['message']}")
                    for d in f.get('details', []):
                        lines.append(f"  {d}")
            lines.append("")

    if review:
        lines += ["---", "", "### 🟡 Revisar antes de aprovar", ""]
        for r in review:
            lines.append(f"<details>")
            lines.append(f"<summary><code>{r['bucket']}</code> — {_review_summary(r)}</summary>")
            lines.append("")
            for f in r['findings']:
                if f['severity'] in (BLOCKED, REVIEW):
                    icon = '❌' if f['severity'] == BLOCKED else '⚠️'
                    lines.append(f"- {icon} **{f['action'].upper()}** — `{f['resource']}`")
                    lines.append(f"  > {f['message']}")
                    for d in f.get('details', []):
                        lines.append(f"  {d}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if info:
        lines += ["---", "", "### 🔵 Só mudanças informativas", ""]
        for r in info:
            msgs = [f['message'] for f in r['findings'] if f['severity'] == INFO]
            lines.append(f"- `{r['bucket']}` — {'; '.join(msgs[:2])}")
        lines.append("")

    if safe:
        lines += [
            f"<details>",
            f"<summary>✅ {len(safe)} bucket(s) sem mudanças relevantes</summary>",
            "",
        ]
        for r in safe:
            lines.append(f"- `{r['bucket']}`")
        lines.append("</details>")

    return '\n'.join(lines)


def _review_summary(result: dict) -> str:
    types = set()
    for f in result['findings']:
        if f['severity'] in (BLOCKED, REVIEW):
            rtype = f['resource'].split('.')[-1] if '.' in f['resource'] else f['resource']
            types.add(rtype.replace('aws_s3_bucket_', '').replace('aws_s3_', ''))
    return ', '.join(sorted(types)) or 'mudanças detectadas'


# ── Carregamento dos planos ───────────────────────────────────────────────────

def find_plan_files(plan_dir: Path, bucket_filter: str | None) -> list[tuple[str, Path]]:
    """Retorna lista de (bucket_name, plan_json_path)."""
    result = []
    for plan_file in sorted(plan_dir.rglob('plan_output.json')):
        bucket_name = _bucket_from_path(plan_file)
        if bucket_filter and bucket_filter not in bucket_name:
            continue
        result.append((bucket_name, plan_file))
    return result


def _bucket_from_path(plan_file: Path) -> str:
    """Extrai nome do bucket do path: .../services/s3/{logical}/{env}/plan_output.json"""
    parts = plan_file.parts
    try:
        s3_idx = next(i for i, p in enumerate(parts) if p == 's3')
        logical = parts[s3_idx + 1]
        env     = parts[s3_idx + 2]
        # Tenta reconstruir nome ECS padrão a partir do repo + logical + env
        repo = next((p for p in parts if re.match(r'ecs-\w+-default-aws-terraform', p)), '')
        team = re.match(r'ecs-(\w+)-default', repo).group(1) if repo else ''
        if team:
            return f"ecs-{team}-{logical}-{env}"
        return f"{logical}-{env}"
    except (StopIteration, IndexError, AttributeError):
        return plan_file.parent.name


# ── GitLab ────────────────────────────────────────────────────────────────────

def post_to_gitlab_mr(mr_url: str, body: str, token: str) -> None:
    m = re.match(r'https?://([^/]+)/(.+)/-/merge_requests/(\d+)', mr_url)
    if not m:
        print(f"URL de MR inválida: {mr_url}", file=sys.stderr)
        return
    host, project_path, mr_iid = m.group(1), m.group(2), m.group(3)
    project_enc = urllib.parse.quote(project_path, safe='')
    api_url = f"https://{host}/api/v4/projects/{project_enc}/merge_requests/{mr_iid}/notes"
    payload = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        api_url, data=payload,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            print(f"Comentário postado na MR: {mr_url}")
    except Exception as e:
        print(f"Erro ao postar na MR: {e}", file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description='Analisa terraform plan -json e classifica mudanças S3 por severidade',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python3 plan_reviewer.py --plan-dir mr_output/
  python3 plan_reviewer.py --bucket ecs-ecred-consumer-dev --plan-dir mr_output/
  python3 plan_reviewer.py --plan-json plan_output.json --output review.md
  python3 plan_reviewer.py --plan-dir mr_output/ --fail-on-blocked  # para CI
        """,
    )
    ap.add_argument('--plan-dir',  type=Path, default=Path('./mr_output'),
                    help='Diretório com subpastas de buckets (padrão: ./mr_output)')
    ap.add_argument('--bucket',    help='Filtrar por nome de bucket')
    ap.add_argument('--plan-json', type=Path,
                    help='Ler diretamente de um arquivo plan_output.json')
    ap.add_argument('--output',    type=Path,
                    help='Salvar relatório em arquivo Markdown')
    ap.add_argument('--post-to-mr', metavar='MR_URL',
                    help='Postar relatório como nota na MR do GitLab')
    ap.add_argument('--fail-on-blocked', action='store_true',
                    help='Exit code 1 se houver buckets bloqueados (útil em CI/CD)')
    ap.add_argument('--fail-on-review', action='store_true',
                    help='Exit code 1 se houver buckets que precisam de revisão')
    args = ap.parse_args()

    results: list[dict] = []

    if args.plan_json:
        try:
            plan = json.loads(args.plan_json.read_text())
        except Exception as e:
            print(f"Erro ao ler {args.plan_json}: {e}", file=sys.stderr)
            return 1
        bucket = args.bucket or _bucket_from_path(args.plan_json)
        results.append(review_plan(plan, bucket))
    else:
        entries = find_plan_files(args.plan_dir, args.bucket)
        if not entries:
            print(
                "Nenhum plan_output.json encontrado.\n"
                "Rode a fase3 do S3_migrate.py para gerar os planos.",
                file=sys.stderr,
            )
            return 1
        for bucket_name, plan_file in entries:
            try:
                plan = json.loads(plan_file.read_text())
                results.append(review_plan(plan, bucket_name))
            except Exception as e:
                print(f"Erro ao ler {plan_file}: {e}", file=sys.stderr)

    if not results:
        print("Nenhum resultado para exibir.", file=sys.stderr)
        return 1

    report = format_report(results)

    if args.output:
        args.output.write_text(report, encoding='utf-8')
        print(f"Relatório salvo em: {args.output}")
    else:
        print(report)

    if args.post_to_mr:
        token = os.environ.get('GITLAB_TOKEN', '')
        if not token:
            print("GITLAB_TOKEN não definido — não foi possível postar na MR", file=sys.stderr)
        else:
            post_to_gitlab_mr(args.post_to_mr, report, token)

    blocked = [r for r in results if r['verdict'] == BLOCKED]
    review  = [r for r in results if r['verdict'] == REVIEW]

    if blocked:
        print(f"\n🔴 {len(blocked)} bucket(s) BLOQUEADOS — não abrir/aprovar MR sem resolver.",
              file=sys.stderr)
    if review:
        print(f"🟡 {len(review)} bucket(s) precisam de revisão humana.", file=sys.stderr)

    if args.fail_on_blocked and blocked:
        return 1
    if args.fail_on_review and (blocked or review):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

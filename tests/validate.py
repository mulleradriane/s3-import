"""
Valida o output do terraform plan e do CHANGES.md para cada cenário.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def check_plan_json(plan_path: Path, scenario: dict) -> list[str]:
    """
    Lê plan_output.json e retorna lista de falhas encontradas.
    Retorna [] se tudo OK.
    """
    failures = []
    expects = scenario.get('expects', {})

    if not plan_path.exists():
        failures.append("plan_output.json não encontrado")
        return failures

    try:
        plan = json.loads(plan_path.read_text())
    except Exception as e:
        failures.append(f"plan_output.json inválido: {e}")
        return failures

    # Extrai todas as resource_changes
    changes = plan.get('resource_changes', [])

    SAFE_DESTROY_TYPES = {
        'aws_sqs_queue_policy',
        'aws_s3_bucket_metric',
        'time_static', 'time_rotating',
        'aws_s3_bucket_acl',
        'random_',
    }

    destroys = []
    replaces = []
    for rc in changes:
        actions = rc.get('change', {}).get('actions', [])
        addr = rc.get('address', '')

        if 'delete' in actions and 'create' not in actions:
            is_safe = any(s in addr for s in SAFE_DESTROY_TYPES)
            if not is_safe:
                destroys.append(addr)

        if actions == ['delete', 'create'] or actions == ['create', 'delete']:
            replaces.append(addr)

    if expects.get('no_destroy_in_plan') and destroys:
        failures.append(f"DESTROY inesperado no plan: {destroys}")

    if expects.get('no_replace_in_plan') and replaces:
        failures.append(f"REPLACE inesperado no plan: {replaces}")

    # Após import, o bucket principal não deve aparecer como create
    if expects.get('no_create_bucket_in_plan'):
        bucket_creates = [
            rc.get('address', '') for rc in changes
            if rc.get('type') == 'aws_s3_bucket'
            and rc.get('name') == 'main'
            and 'create' in rc.get('change', {}).get('actions', [])
            and 'delete' not in rc.get('change', {}).get('actions', [])
        ]
        if bucket_creates:
            failures.append(
                f"aws_s3_bucket.main ainda como create após import: {bucket_creates}"
            )

    return failures


def check_main_tf(tf_path: Path, scenario: dict) -> list[str]:
    """Valida o conteúdo do main.tf gerado."""
    failures = []
    expects = scenario.get('expects', {})

    if not tf_path.exists():
        failures.append("main.tf não encontrado")
        return failures

    content = tf_path.read_text()

    for expected_str in expects.get('tf_contains', []):
        if expected_str.lower() not in content.lower():
            failures.append(f"main.tf não contém: {expected_str!r}")

    if expects.get('no_lifecycle_rules_in_tf'):
        if 'lifecycle_rules' in content and '= []' not in content:
            # Verifica se lifecycle_rules está setado com valor não-vazio
            m = re.search(r'lifecycle_rules\s*=\s*(\[.*?\])', content, re.DOTALL)
            if m and m.group(1).strip() not in ('[]', '[ ]'):
                failures.append("main.tf contém lifecycle_rules não-vazio (esperado sem lifecycle)")

    return failures


def check_changes_md(md_path: Path, scenario: dict) -> list[str]:
    """Valida o CHANGES.md gerado."""
    failures = []
    expects = scenario.get('expects', {})

    if not md_path.exists():
        # CHANGES.md pode não existir se não há mudanças — não é falha obrigatória
        return failures

    content = md_path.read_text().lower()

    for expected_str in expects.get('changes_md_contains', []):
        if expected_str.lower() not in content:
            failures.append(f"CHANGES.md não contém: {expected_str!r}")

    return failures


def check_import_completeness(tf_dir: Path, scenario: dict) -> list[str]:
    """
    Lê o terraform.tfstate local e verifica se aws_s3_bucket.main foi importado.
    Retorna lista de falhas.
    """
    failures = []
    # O state pode estar no path padrão ou num subdir (state_old/state_new)
    state_candidates = [
        tf_dir / "terraform.tfstate",
        tf_dir / "state_old" / "terraform.tfstate",
        tf_dir / "state_new" / "terraform.tfstate",
    ]
    state_path = next((p for p in state_candidates if p.exists()), None)
    if not state_path:
        failures.append("terraform.tfstate não encontrado — import pode ter sido pulado")
        return failures

    try:
        state = json.loads(state_path.read_text())
    except Exception as e:
        failures.append(f"terraform.tfstate inválido: {e}")
        return failures

    resources_in_state = {
        (r.get('module', ''), r.get('type', ''), r.get('name', ''))
        for r in state.get('resources', [])
    }

    # Recurso mínimo que DEVE estar no state após import
    bucket_present = any(
        rtype == 'aws_s3_bucket' and rname == 'main'
        for _, rtype, rname in resources_in_state
    )
    if not bucket_present:
        failures.append(
            "Import incompleto: aws_s3_bucket.main não está no state "
            f"(recursos presentes: {len(resources_in_state)})"
        )

    return failures


def check_legacy_state_detection(state_path: Path) -> bool:
    """
    Retorna True se o state file é TF < 0.13 (legacy provider format).
    Replica a lógica de check_tfstate_version de S3_migrate.py para uso local.
    """
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text())
        version = state.get('terraform_version', '0.0.0')
        resources = state.get('resources', [])

        # Formato legado: "provider.aws" sem namespace registry.terraform.io
        for res in resources:
            prov = res.get('provider', '')
            if prov and 'registry.terraform.io' not in prov and 'provider.' in prov:
                return True

        parts = version.split('.')
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        return major == 0 and minor < 13
    except Exception:
        return False


def check_extraction(config_path: Path) -> list[str]:
    """Verifica se a extração do bucket foi bem-sucedida."""
    failures = []

    if not config_path.exists():
        failures.append("arquivo .s3config.json não encontrado")
        return failures

    try:
        cfg = json.loads(config_path.read_text())
    except Exception as e:
        failures.append(f"s3config.json inválido: {e}")
        return failures

    errors = cfg.get('_errors', {})
    critical_fields = ['versioning', 'encryption', 'lifecycle', 'tagging']
    for f in critical_fields:
        if f in errors:
            failures.append(f"Extração falhou para '{f}': {errors[f]}")

    if cfg.get('_bucket') is None:
        failures.append("Campo _bucket ausente no config")

    return failures


def summarize(results: list[dict]) -> None:
    """Imprime resumo final dos resultados."""
    total  = len(results)
    passed = sum(1 for r in results if r['status'] == 'PASS')
    failed = sum(1 for r in results if r['status'] == 'FAIL')
    errors = sum(1 for r in results if r['status'] == 'ERROR')

    print(f"\n{'=' * 65}")
    print(f"  RESULTADO FINAL — {total} cenários")
    print(f"  ✅ PASS:  {passed}")
    print(f"  ❌ FAIL:  {failed}")
    print(f"  💥 ERROR: {errors}")
    print(f"{'=' * 65}\n")

    for r in results:
        icon = {'PASS': '✅', 'FAIL': '❌', 'ERROR': '💥'}.get(r['status'], '?')
        print(f"  {icon} [{r['scenario_id']:15s}] {r['description'][:45]}")
        for f in r.get('failures', []):
            print(f"       → {f}")
        if r.get('plan_summary'):
            print(f"       plan: {r['plan_summary']}")
    print()

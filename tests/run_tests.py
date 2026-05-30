#!/usr/bin/env python3
"""
Orquestrador de testes do pipeline S3 Migration.

Para cada cenário:
  1. Extrai config do bucket (s3_config_extractor)
  2. Gera main.tf + CHANGES.md (s3_main_tf_gen)
  3. Substitui source do módulo → mock_bp local
  4. terraform init + plan
  5. Valida: sem destroys inesperados, variáveis corretas, CHANGES.md correto

Uso:
  python3 tests/run_tests.py --prefix s3mig-test --csv tests/test_levantamento.csv
  python3 tests/run_tests.py --prefix s3mig-test --csv tests/test_levantamento.csv --scenario plain aes256
  python3 tests/run_tests.py --prefix s3mig-test --csv tests/test_levantamento.csv --no-plan
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR   = Path(__file__).parent
SCRIPTS_DIR = TESTS_DIR.parent
MOCK_BP_DIR = TESTS_DIR / "mock_bp"
OUTPUT_DIR  = TESTS_DIR / "output"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from scenarios import SCENARIOS


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _run(cmd: str, cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def _patch_module_source(tf_path: Path) -> None:
    """Substitui o source do módulo BP pelo mock_bp local."""
    content = tf_path.read_text()
    # Substitui qualquer URL git:: ... por caminho local absoluto
    patched = re.sub(
        r'source\s*=\s*"git::[^"]+"',
        f'source = "{MOCK_BP_DIR}"',
        content
    )
    if patched == content:
        # Tenta formato sem git::
        patched = re.sub(
            r'(source\s*=\s*")([^"]*gitlab[^"]*|[^"]*blueprint[^"]*|[^"]*bp-aws-s3[^"]*)"',
            f'\\1{MOCK_BP_DIR}"',
            content
        )
    tf_path.write_text(patched)


def _write_provider_tf(work_dir: Path, region: str) -> None:
    """Adiciona apenas o provider block — required_providers já está em versions.tf."""
    provider_tf = work_dir / "provider.tf"
    provider_tf.write_text(f'provider "aws" {{\n  region = "{region}"\n}}\n')


def _write_backend_tf(work_dir: Path, state_path: str | None = None) -> None:
    """Substitui backend.tf gerado (aponta para S3) por backend local para testes."""
    generated = work_dir / "backend.tf"
    if generated.exists():
        generated.unlink()
    for f in work_dir.glob("backend_override.tf"):
        f.unlink()
    if state_path:
        body = f'terraform {{\n  backend "local" {{\n    path = "{state_path}"\n  }}\n}}\n'
    else:
        body = 'terraform {\n  backend "local" {}\n}\n'
    (work_dir / "backend_local.tf").write_text(body)


def _run_import(tf_dir: Path) -> tuple[bool, str]:
    """Roda _import_commands.sh para trazer recursos existentes ao state."""
    import_sh = tf_dir / "_import_commands.sh"
    if not import_sh.exists():
        return False, "_import_commands.sh não encontrado"
    import_sh.chmod(0o755)
    return _run("bash _import_commands.sh 2>&1", cwd=tf_dir, timeout=300)


def _seed_legacy_state(tf_dir: Path, bucket_name: str, tf_version: str) -> Path:
    """Cria state sintético no formato TF 0.12 (format version 3, provider legacy)."""
    import uuid
    state = {
        "version": 3,
        "terraform_version": tf_version,
        "serial": 1,
        "lineage": str(uuid.uuid4()),
        "outputs": {},
        "resources": [
            {
                "mode": "managed",
                "type": "aws_s3_bucket",
                "name": "main",
                "provider": "provider.aws",
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {
                            "bucket": bucket_name,
                            "id": bucket_name,
                            "region": "us-east-1",
                        }
                    }
                ]
            }
        ]
    }
    state_path = tf_dir / "terraform.tfstate"
    state_path.write_text(json.dumps(state, indent=2))
    return state_path


def _extract_plan_summary(plan_json_path: Path) -> str:
    """Extrai resumo legível do plan_output.json."""
    try:
        plan = json.loads(plan_json_path.read_text())
        changes = plan.get('resource_changes', [])
        counts = {'create': 0, 'update': 0, 'delete': 0, 'no-op': 0}
        for rc in changes:
            actions = rc.get('change', {}).get('actions', [])
            if actions == ['no-op']:
                counts['no-op'] += 1
            elif 'delete' in actions and 'create' in actions:
                counts['update'] += 1  # replace
            elif 'delete' in actions:
                counts['delete'] += 1
            elif 'create' in actions:
                counts['create'] += 1
            elif 'update' in actions:
                counts['update'] += 1
        parts = [f"+{counts['create']}" if counts['create'] else '',
                 f"~{counts['update']}" if counts['update'] else '',
                 f"-{counts['delete']}" if counts['delete'] else '']
        return ' '.join(p for p in parts if p) or 'no changes'
    except Exception:
        return '?'


def _check_discovery_tier(bucket: str, team: str, env: str, asset: str,
                           configs_dir: Path,
                           csv_blockers: str = '') -> tuple[str, list[str]]:
    """Chama discover_one e retorna (tier_real, script_flags)."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from s3_discovery import discover_one
    row = {
        'bucket_name': bucket, 'team': team, 'env': env,
        'asset_category': asset, 'category': 'A',
        'blockers': csv_blockers,
    }
    r = discover_one(row=row, all_rows=[row], configs_dir=configs_dir,
                     do_extract=False, force_extract=False)
    return r['tier'], r.get('script_flags', [])


# ─── Pipeline para um cenário ─────────────────────────────────────────────────

def run_scenario(sc: dict, prefix: str, region: str, run_plan: bool, args) -> dict:
    from scenarios import SCENARIOS

    sc_id = sc['id']
    env   = sc.get('env', 'dev')
    team  = sc.get('team', 's3mig')
    asset = sc.get('asset_cat', 'Productive data')
    name  = f"s3-{prefix}-{sc_id}" if sc.get('legacy_name') else f"{prefix}-{sc_id}"

    result = {
        'scenario_id': sc_id,
        'description': sc['description'],
        'bucket':      name,
        'status':      'PASS',
        'failures':    [],
        'plan_summary': '',
        'steps':       {},
    }

    work_dir = OUTPUT_DIR / sc_id
    work_dir.mkdir(parents=True, exist_ok=True)
    configs_dir = work_dir / "configs"
    configs_dir.mkdir(exist_ok=True)

    print(f"\n  {'─' * 55}")
    print(f"  [{sc_id}] {sc['description']}")
    print(f"  Bucket: {name} | env: {env} | asset: {asset}")

    # ── Passo 1: Extração ──────────────────────────────────────────────────────
    print(f"  1/4 Extraindo config da AWS...", end=' ', flush=True)
    ok, out = _run(
        f"python3 {SCRIPTS_DIR}/s3_config_extractor.py "
        f"--bucket {name} --output-dir {configs_dir}",
        cwd=SCRIPTS_DIR
    )
    config_file = configs_dir / f"{name}.s3config.json"
    result['steps']['extract'] = 'OK' if ok and config_file.exists() else 'FAIL'

    if not config_file.exists():
        result['failures'].append(f"Extração falhou — bucket '{name}' não encontrado ou sem acesso")
        result['status'] = 'ERROR'
        print("❌")
        return result
    print("✅")

    # Valida extração
    from validate import check_extraction
    extract_fails = check_extraction(config_file)
    if extract_fails:
        result['failures'].extend(extract_fails)
        result['status'] = 'FAIL'

    # ── Passo 1b: Discovery tier ───────────────────────────────────────────────
    expected_tier = sc.get('tier')
    if expected_tier:
        print(f"  1b   Discovery tier...", end=' ', flush=True)
        try:
            actual_tier, tier_flags = _check_discovery_tier(
                name, team, env, asset, configs_dir,
                csv_blockers=sc.get('csv_blockers', '')
            )
            result['tier_actual'] = actual_tier
            result['tier_flags']  = tier_flags
            if actual_tier != expected_tier:
                result['failures'].append(
                    f"Tier incorreto: esperado {expected_tier}, got {actual_tier} "
                    f"(flags: {','.join(tier_flags) or 'nenhum'})"
                )
                result['status'] = 'FAIL'
                print(f"❌ ({actual_tier} ≠ {expected_tier})")
            else:
                print(f"✅ ({actual_tier})")
            # Buckets BLOCK confirmados: não migrar, encerra o cenário aqui
            if actual_tier == 'BLOCK' and expected_tier == 'BLOCK':
                result['plan_summary'] = '(BLOCK — migration stopped)'
                result['steps']['tier'] = 'BLOCK-OK'
                return result
        except Exception as e:
            result['failures'].append(f"Discovery tier falhou: {e}")
            result['status'] = 'FAIL'
            print(f"❌ {e}")

    # ── Passo 2: Geração do main.tf ────────────────────────────────────────────
    print(f"  2/4 Gerando main.tf + CHANGES.md...", end=' ', flush=True)

    try:
        import json as _json
        from s3_main_tf_gen import gen_all_files

        cfg = _json.loads(config_file.read_text())
        files = gen_all_files(
            bucket_name=name,
            team=team,
            env=env,
            asset_cat=asset,
            cfg=cfg,
            state_bucket="local-test",
            state_region=region,
            log_bucket="ecs-logging-test",
            ticket="S3MIG-TEST",
        )

        # Salva arquivos gerados em work_dir/tf/
        tf_dir = work_dir / "tf"
        tf_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            fpath = tf_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content or "")

        result['steps']['generate'] = 'OK'
        print("✅")
    except Exception as e:
        import traceback
        result['failures'].append(f"Geração falhou: {e}")
        result['steps']['generate'] = 'FAIL'
        result['status'] = 'FAIL'
        print(f"❌ {e}")
        (work_dir / "generate_error.txt").write_text(traceback.format_exc())
        return result

    tf_path = tf_dir / "main.tf"
    if not tf_path.exists():
        result['failures'].append("main.tf não encontrado após geração")
        result['status'] = 'FAIL'
        return result

    # Valida conteúdo do main.tf
    from validate import check_main_tf, check_changes_md
    tf_fails = check_main_tf(tf_path, sc)
    result['failures'].extend(tf_fails)
    if tf_fails:
        result['status'] = 'FAIL'

    # Valida CHANGES.md
    changes_path = tf_dir / "CHANGES.md"
    md_fails = check_changes_md(changes_path, sc)
    result['failures'].extend(md_fails)
    if md_fails:
        result['status'] = 'FAIL'

    if not run_plan:
        result['plan_summary'] = '(plan pulado)'
        return result

    # ── Passo 3: Terraform init ────────────────────────────────────────────────
    steps_total = '5' if sc.get('run_import') or sc.get('seed_legacy_state') else '4'
    if sc.get('run_state_migrate'):
        steps_total = '6'

    print(f"  3/{steps_total} terraform init...", end=' ', flush=True)

    _patch_module_source(tf_path)
    _write_provider_tf(tf_dir, region)

    # State migration: usa backend com path explícito para poder migrar depois
    if sc.get('run_state_migrate'):
        # Limpa state dirs E .terraform de runs anteriores (backend cache)
        import shutil
        for d in ['state_old', 'state_new', '.terraform']:
            p = tf_dir / d
            if p.exists():
                shutil.rmtree(p)
        for f in ['terraform.tfstate', 'terraform.tfstate.backup',
                  '.terraform.lock.hcl']:
            p = tf_dir / f
            if p.exists():
                p.unlink()
        (tf_dir / "state_old").mkdir(exist_ok=True)
        old_state_path = str(tf_dir / "state_old" / "terraform.tfstate")
        _write_backend_tf(tf_dir, state_path=old_state_path)
    else:
        _write_backend_tf(tf_dir)

    # State legado 0.12: seed estado sintético antes do init
    if sc.get('seed_legacy_state'):
        from validate import check_legacy_state_detection
        seeded_path = _seed_legacy_state(tf_dir, name, sc['seed_legacy_state'])
        detected = check_legacy_state_detection(seeded_path)
        result['steps']['state_detection'] = 'OK (legacy)' if detected else 'FAIL (não detectou)'
        if sc.get('expects', {}).get('state_detected_as_legacy') and not detected:
            result['failures'].append(
                f"State TF {sc['seed_legacy_state']} não foi detectado como legacy"
            )
            result['status'] = 'FAIL'
        elif detected:
            print(f"\n  ℹ️  State TF {sc['seed_legacy_state']} detectado como legacy — import limpo")

    ok, out = _run("terraform init -input=false -no-color 2>&1", cwd=tf_dir, timeout=180)
    result['steps']['tf_init'] = 'OK' if ok else 'FAIL'
    if not ok:
        result['failures'].append(f"terraform init falhou: {out[-500:]}")
        result['status'] = 'FAIL'
        print("❌")
        (work_dir / "tf_init_error.txt").write_text(out)
        return result
    print("✅")

    # ── Passo 3b: Terraform import (opcional) ─────────────────────────────────
    if sc.get('run_import'):
        step_n = '3b'
        print(f"  {step_n}/{steps_total} terraform import...", end=' ', flush=True)
        ok_imp, out_imp = _run_import(tf_dir)
        already_managed = any(x in out_imp for x in [
            'Resource already managed', 'already exists in state',
            'already managed by Terraform', 'Cannot import non-existent',
        ])
        if already_managed:
            result['steps']['tf_import'] = 'SKIP (já no state)'
            print("⏭  (já no state)")
        elif ok_imp:
            result['steps']['tf_import'] = 'OK'
            print("✅")
        else:
            # Import parcial é aceitável — alguns sub-recursos podem não existir ainda
            result['steps']['tf_import'] = 'PARTIAL'
            (work_dir / "import_output.txt").write_text(out_imp)
            print("⚠️  (parcial)")

    # ── Passo 4: Terraform plan ────────────────────────────────────────────────
    print(f"  4/{steps_total} terraform plan...", end=' ', flush=True)

    def _do_plan(suffix: str = '') -> tuple[bool, str]:
        ok_p, out_p = _run(
            "terraform plan -input=false -no-color -out=tfplan.binary 2>&1",
            cwd=tf_dir, timeout=300
        )
        if ok_p:
            ok_j, out_j = _run("terraform show -json tfplan.binary 2>&1", cwd=tf_dir)
            if ok_j and out_j.strip().startswith('{'):
                pjp = work_dir / f"plan_output{suffix}.json"
                pjp.write_text(out_j)
                _run("rm -f tfplan.binary", cwd=tf_dir)
                return True, str(pjp)
        _run("rm -f tfplan.binary", cwd=tf_dir)
        return False, out_p

    ok_plan, plan_data = _do_plan()
    result['steps']['tf_plan'] = 'OK' if ok_plan else 'FAIL'

    if not ok_plan:
        result['failures'].append(f"terraform plan falhou: {plan_data[-500:]}")
        result['status'] = 'FAIL'
        (work_dir / "tf_plan_error.txt").write_text(plan_data)
        print("❌")
        return result

    plan_json_path = Path(plan_data)
    from validate import check_plan_json, check_import_completeness
    plan_fails = check_plan_json(plan_json_path, sc)
    result['failures'].extend(plan_fails)
    if plan_fails:
        result['status'] = 'FAIL'
    result['plan_summary'] = _extract_plan_summary(plan_json_path)

    # Idempotência: segundo plan deve produzir exatamente o mesmo resumo
    ok_plan2, plan_data2 = _do_plan(suffix='_idem')
    if ok_plan2:
        summary2 = _extract_plan_summary(Path(plan_data2))
        if summary2 != result['plan_summary']:
            result['failures'].append(
                f"Idempotência falhou: plan1={result['plan_summary']}, plan2={summary2}"
            )
            result['status'] = 'FAIL'
        result['plan_summary'] += f' (idem: {summary2})'

    # Import completeness: verifica sub-recursos no state após import
    if sc.get('run_import'):
        imp_fails = check_import_completeness(tf_dir, sc)
        result['failures'].extend(imp_fails)
        if imp_fails:
            result['status'] = 'FAIL'

    print(f"✅ ({_extract_plan_summary(plan_json_path)})")

    # ── Passo 5: State migration (opcional) ───────────────────────────────────
    if sc.get('run_state_migrate'):
        print(f"  5/{steps_total} terraform init -migrate-state...", end=' ', flush=True)

        # Aponta backend para novo path
        new_state_path = str(tf_dir / "state_new" / "terraform.tfstate")
        (tf_dir / "state_new").mkdir(exist_ok=True)
        _write_backend_tf(tf_dir, state_path=new_state_path)

        ok_m, out_m = _run(
            "terraform init -migrate-state -force-copy -input=false -no-color 2>&1",
            cwd=tf_dir, timeout=180
        )
        result['steps']['tf_migrate'] = 'OK' if ok_m else 'FAIL'
        if not ok_m:
            result['failures'].append(f"init -migrate-state falhou: {out_m[-500:]}")
            result['status'] = 'FAIL'
            print("❌")
            (work_dir / "tf_migrate_error.txt").write_text(out_m)
            return result
        print("✅")

        # Plan pós-migração — state deve estar preservado
        print(f"  6/{steps_total} terraform plan (pós-migrate)...", end=' ', flush=True)
        ok_plan2, plan_data2 = _do_plan(suffix='_post_migrate')
        result['steps']['tf_plan_post_migrate'] = 'OK' if ok_plan2 else 'FAIL'

        if ok_plan2:
            plan_json_path2 = Path(plan_data2)
            post_fails = check_plan_json(plan_json_path2, sc)
            result['failures'].extend(post_fails)
            if post_fails:
                result['status'] = 'FAIL'
            post_summary = _extract_plan_summary(plan_json_path2)
            result['plan_summary'] += f' → {post_summary}'
            print(f"✅ ({post_summary})")
        else:
            result['failures'].append(f"plan pós-migrate falhou: {plan_data2[-500:]}")
            result['status'] = 'FAIL'
            print("❌")

    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description='Roda testes do pipeline S3 Migration')
    ap.add_argument('--prefix',   required=True, help='Prefixo dos buckets de teste')
    ap.add_argument('--region',   default='us-east-1')
    ap.add_argument('--scenario', nargs='+', help='Filtrar cenários')
    ap.add_argument('--no-plan',  action='store_true',
                    help='Pula terraform init/plan (só extração + geração)')
    ap.add_argument('--fail-fast', action='store_true',
                    help='Para no primeiro erro')
    args = ap.parse_args()

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if s['id'] in args.scenario]
    if not scenarios:
        print("Nenhum cenário selecionado.", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 65}")
    print(f"  S3 MIGRATION TEST SUITE")
    print(f"  Prefixo: {args.prefix} | Região: {args.region}")
    print(f"  Cenários: {len(scenarios)} | Plan: {'sim' if not args.no_plan else 'não'}")
    print(f"{'=' * 65}")

    results = []
    for sc in scenarios:
        try:
            r = run_scenario(sc, args.prefix, args.region, not args.no_plan, args)
            results.append(r)
            if args.fail_fast and r['status'] != 'PASS':
                print(f"\n  Parando por --fail-fast (falha em '{sc['id']}')")
                break
        except KeyboardInterrupt:
            print("\n  Interrompido pelo usuário")
            break
        except Exception as e:
            results.append({
                'scenario_id': sc['id'],
                'description': sc['description'],
                'bucket': f"{args.prefix}-{sc['id']}",
                'status': 'ERROR',
                'failures': [str(e)],
                'plan_summary': '',
            })

    from validate import summarize
    summarize(results)

    # Salva resultado JSON
    result_file = OUTPUT_DIR / "test_results.json"
    result_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"  Resultados completos: {result_file}")

    failed = sum(1 for r in results if r['status'] != 'PASS')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""
s3.py — CLI unificado para migração de buckets S3 → Terraform Blueprint

Subcomandos:
  check      Valida pré-requisitos (AWS, Terraform, GitLab token)
  discover   Classifica buckets em BLOCK / REVIEW / AUTO antes de migrar
  run        Pipeline completo: extrai → gera → plan → review → abre MRs
  review     Analisa plans existentes e exibe relatório de segurança
  status     Mostra estado atual (processados, bloqueados, pendentes)

Fluxo recomendado:
  python3 s3.py check
  python3 s3.py discover --env hml --csv levantamento.csv
  python3 s3.py run --env hml --csv levantamento.csv --dry-run
  python3 s3.py review
  python3 s3.py run --env hml --csv levantamento.csv --ticket SRE-1234
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
MIGRATE_PY   = SCRIPT_DIR / "S3_migrate.py"
DISCOVERY_PY = SCRIPT_DIR / "s3_discovery.py"
REVIEWER_PY  = SCRIPT_DIR / "plan_reviewer.py"

# Defaults vindos do S3_migrate.py
_GITLAB_URL    = "https://gitlab.ecsbr.net"
_STATE_BUCKET  = "387979423286-tfstate"
_STATE_REGION  = "us-east-1"
_OUTPUT_DIR    = "./mr_output"
_REPOS_DIR     = "./repos"


# Produtos/times conhecidos (bucket naming: ecs-{product}-{logical}-{env})
KNOWN_PRODUCTS = sorted([
    "antifraude", "auth", "b2b", "chatbot", "collection", "core", "crawler",
    "cross", "ctools", "dataops", "ecred", "engineering", "ewallet",
    "fraudtools", "gac", "ia", "id", "infra", "insurance", "ipaas", "lno",
    "mobile", "monitoring", "nogordio", "observability", "ops", "partnerportal",
    "platform", "premium", "score", "seguros", "serasabox", "sharedservices",
    "splunk", "staffengineering", "web",
])

_LEGACY_OUTROS_PREFIXES = ("ecsops-", "datadog-", "logs-")
_ENVS = {"dev", "hml", "prd", "staging", "sandbox", "qa"}


def classify_bucket_name(name: str, team: str | None = None) -> tuple[str, str | None]:
    """
    Classifica o padrão de nomenclatura de um bucket S3.

    Retorna (categoria, divergência_ou_None).
    Categorias:
      padrao_ecs       — ecs-{product}-{logical}-{env}           (padrão atual)
      legado_s3        — s3-{team}-* ou *.ecsbr.net              (herança legada)
      legado_outros    — ecsops-*, datadog-*, logs-*             (infraestrutura ops)
      legado_nome_direto — {product}-* sem prefixo ecs/s3        (nome direto)
      legado_accountid — {accountId}-*                           (gerado automaticamente)
      ecs_outro_time   — ecs-{prefix} mas prefix ≠ team tag      (divergência de time)
      unknown          — nenhum padrão reconhecido
    """
    import re

    if name.endswith(".ecsbr.net") or name.startswith("s3-"):
        return "legado_s3", None

    if any(name.startswith(p) for p in _LEGACY_OUTROS_PREFIXES):
        return "legado_outros", None

    if re.match(r"^\d{12}-", name):
        return "legado_accountid", None

    # Padrão ecs-{product}-{logical}-{env}
    m = re.match(r"^ecs-([^-]+)-(.+)-([^-]+)$", name)
    if m:
        prefix, logical, env = m.group(1), m.group(2), m.group(3)
        if env in _ENVS:
            if prefix in KNOWN_PRODUCTS:
                # Verifica se o team tag bate
                if team and team != prefix:
                    return "ecs_outro_time", (
                        f"nome sugere produto '{prefix}' mas tag Team='{team}'"
                    )
                return "padrao_ecs", None
            else:
                return "ecs_outro_time", f"prefixo '{prefix}' não é produto conhecido"

    # Padrão legado_nome_direto — {product}-*
    for prod in KNOWN_PRODUCTS:
        if name == prod or name.startswith(f"{prod}-"):
            return "legado_nome_direto", None

    return "unknown", None


def _add_product_arg(parser: argparse.ArgumentParser) -> None:
    """Adiciona --product (alias de --team) com choices dos produtos conhecidos."""
    parser.add_argument(
        "--product",
        metavar="PRODUTO",
        help=(
            "Produto/time a processar — filtra buckets pelo prefixo do nome "
            "(ex: lno → ecs-lno-*). "
            f"Conhecidos: {', '.join(KNOWN_PRODUCTS)}"
        ),
    )


def _resolve_team(args: argparse.Namespace) -> str | None:
    """Retorna o time efetivo: --product tem precedência sobre --team."""
    return getattr(args, "product", None) or getattr(args, "team", None)


# ── Utilitários ───────────────────────────────────────────────────────────────

def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _ok(label: str) -> None:
    print(f"  ✅ {label}")


def _fail(label: str) -> None:
    print(f"  ❌ {label}", file=sys.stderr)


def _warn(label: str) -> None:
    print(f"  ⚠️  {label}")


def _header(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _python() -> str:
    return sys.executable


def _exec(script: Path, extra_args: list[str]) -> int:
    """Executa um script Python filho mostrando sua saída em tempo real."""
    cmd = [_python(), str(script)] + extra_args
    proc = subprocess.run(cmd)
    return proc.returncode


# ── check ─────────────────────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace) -> int:
    _header("Validando pré-requisitos")
    ok = True

    # Python 3.9+
    if sys.version_info >= (3, 9):
        _ok(f"Python {sys.version.split()[0]}")
    else:
        _fail(f"Python {sys.version.split()[0]} — requer 3.9+")
        ok = False

    # Scripts do projeto
    for script in [MIGRATE_PY, DISCOVERY_PY, REVIEWER_PY]:
        if script.exists():
            _ok(f"{script.name} encontrado")
        else:
            _fail(f"{script.name} não encontrado em {SCRIPT_DIR}")
            ok = False

    # AWS CLI
    r = _run(["aws", "--version"])
    if r.returncode == 0:
        _ok(f"AWS CLI — {r.stdout.strip() or r.stderr.strip()}")
    else:
        _fail("AWS CLI não encontrado — instale: https://aws.amazon.com/cli/")
        ok = False

    # AWS credentials
    r = _run(["aws", "sts", "get-caller-identity", "--output", "json"])
    if r.returncode == 0:
        try:
            identity = json.loads(r.stdout)
            _ok(f"AWS identity: conta {identity.get('Account')} / {identity.get('Arn','?').split('/')[-1]}")
        except Exception:
            _ok("AWS credentials OK")
    else:
        _fail("AWS credentials inválidas — rode: aws sso login")
        ok = False

    # Terraform
    r = _run(["terraform", "version", "-json"])
    if r.returncode == 0:
        try:
            tf_info = json.loads(r.stdout)
            _ok(f"Terraform {tf_info.get('terraform_version','?')}")
        except Exception:
            _ok("Terraform OK")
    else:
        _fail("Terraform não encontrado — instale via tfenv ou download direto")
        ok = False

    # GitLab token
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        _ok(f"GITLAB_TOKEN definido ({len(token)} chars)")
    else:
        _warn("GITLAB_TOKEN não definido — necessário para abrir MRs")

    # git
    r = _run(["git", "--version"])
    if r.returncode == 0:
        _ok(f"git OK")
    else:
        _fail("git não encontrado")
        ok = False

    print()
    if ok:
        print("  Ambiente pronto. Próximo passo:")
        print("    python3 s3.py discover --env dev --csv levantamento.csv")
    else:
        print("  Corrija os itens acima antes de continuar.", file=sys.stderr)
    return 0 if ok else 1


# ── discover ──────────────────────────────────────────────────────────────────

def cmd_discover(args: argparse.Namespace) -> int:
    _header(f"Discovery — {args.env.upper()}")

    if not Path(args.csv).exists():
        _fail(f"CSV não encontrado: {args.csv}")
        return 1

    team = _resolve_team(args)
    extra = [
        "--csv", args.csv,
        "--env", args.env,
        "--out-dir", args.output_dir,
        "--parallel", str(args.parallel),
    ]
    if team:
        extra += ["--team", team]
    if args.extract:
        extra += ["--extract"]
    if args.force_extract:
        extra += ["--force-extract"]

    print(f"  CSV: {args.csv} | env: {args.env}")
    if team:
        print(f"  Produto/time: {team}")
    print()

    # Analisa padrões de nomenclatura antes do discovery
    _naming_analysis(args.csv, team, args.env)

    rc = _exec(DISCOVERY_PY, extra)
    if rc == 0:
        report = Path(args.output_dir) / "discovery_report.md"
        if report.exists():
            print(f"\n  Relatório salvo em: {report}")
        print("\n  Próximo passo:")
        print(f"    python3 s3.py run --env {args.env} --csv {args.csv} --dry-run")
    return rc


def _naming_analysis(csv_path: str, team: str | None, env: str) -> None:
    """Lê o CSV e imprime resumo de padrões de nomenclatura + divergências."""
    import csv as _csv
    from collections import Counter

    counts: Counter[str] = Counter()
    divergences: list[str] = []

    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                name = (row.get("bucket") or row.get("Bucket") or row.get("name") or "").strip()
                row_team = (row.get("team") or row.get("Team") or row.get("time") or "").strip() or team
                bucket_env = (row.get("env") or row.get("Env") or row.get("environment") or "").strip()

                if not name:
                    continue
                # Filtra por env se especificado
                if env != "all" and bucket_env and bucket_env != env:
                    continue
                if env != "all" and team and row_team and row_team != team:
                    continue

                cat, div = classify_bucket_name(name, row_team or None)
                counts[cat] += 1
                if div:
                    divergences.append(f"  ⚠️  {name}  →  {div}")
    except Exception as e:
        _warn(f"Não foi possível analisar CSV para nomenclatura: {e}")
        return

    if not counts:
        return

    total = sum(counts.values())
    print(f"  Análise de nomenclatura ({total} buckets):")
    categories = [
        ("padrao_ecs",        "✅ Padrão ecs-{produto}-{logico}-{env}"),
        ("legado_s3",         "🟠 Legado s3-* / *.ecsbr.net"),
        ("ecs_outro_time",    "🟡 ecs-* com time divergente"),
        ("legado_nome_direto","🟡 Nome direto sem prefixo"),
        ("legado_outros",     "🟠 Legado ecsops-* / datadog-* / logs-*"),
        ("legado_accountid",  "🟠 Prefixo accountId"),
        ("unknown",           "❓ Padrão desconhecido"),
    ]
    for cat, label in categories:
        n = counts.get(cat, 0)
        if n:
            print(f"    {label}: {n}")

    if divergences:
        print(f"\n  Divergências detectadas ({len(divergences)}):")
        for d in divergences[:20]:
            print(d)
        if len(divergences) > 20:
            print(f"    ... e mais {len(divergences) - 20} divergências")
    print()


# ── run ───────────────────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    env  = args.env
    mode = "DRY-RUN" if args.dry_run else "APPROVE"

    _header(f"Pipeline S3 — {env.upper()} [{mode}]")

    # Validações básicas
    if not Path(args.csv).exists():
        _fail(f"CSV não encontrado: {args.csv}")
        return 1

    if not args.dry_run and not args.ticket:
        _fail("--ticket é obrigatório no modo approve (ex: --ticket SRE-1234)")
        return 1

    if env == "prd" and not args.confirm_prd and not args.dry_run:
        _fail("Para processar PRD use --confirm-prd (ambiente crítico)")
        return 1

    # Mostra resumo do que vai rodar
    team = _resolve_team(args)

    print(f"  CSV:     {args.csv}")
    print(f"  Env:     {env}")
    if team:        print(f"  Produto: {team}")
    if args.bucket: print(f"  Bucket:  {args.bucket}")
    if args.ticket: print(f"  Ticket: {args.ticket}")
    if args.max_cache_age:
        print(f"  Cache:  re-extrai se > {args.max_cache_age}h")
    if args.auto_confirm:
        print(f"  Modo:   auto-confirm (sem pausas por bucket)")
    print()

    # Monta args para S3_migrate.py
    extra: list[str] = [
        "--csv",        args.csv,
        "--env",        env,
        "--output-dir", args.output_dir,
        "--repos-dir",  args.repos_dir,
        "--gitlab-url", args.gitlab_url,
    ]

    if args.dry_run:
        extra += ["--dry-run-full"]
    else:
        extra += ["--approve"]

    if team:               extra += ["--team",          team]
    if args.bucket:        extra += ["--bucket",        args.bucket]
    if args.ticket:        extra += ["--ticket",        args.ticket]
    if args.auto_confirm:  extra += ["--auto-confirm"]
    if args.confirm_prd:   extra += ["--confirm-prd"]
    if args.one_mr_per_bucket: extra += ["--one-mr-per-bucket"]
    if args.max_cache_age: extra += ["--max-cache-age", str(args.max_cache_age)]
    if args.gitlab_token:  extra += ["--gitlab-token",  args.gitlab_token]
    if args.parallel:      extra += ["--parallel",      str(args.parallel)]
    if getattr(args, "exclude", None):
        extra += ["--exclude"] + args.exclude

    rc = _exec(MIGRATE_PY, extra)

    # Após o run, chama o reviewer automaticamente se houver plans
    plan_files = list(Path(args.output_dir).rglob("plan_output.json"))
    if plan_files:
        _header("Review automático dos plans")
        review_out = Path(args.output_dir) / "plan_review.md"
        reviewer_args = [
            "--plan-dir", args.output_dir,
            "--output",   str(review_out),
        ]
        if args.bucket:
            reviewer_args += ["--bucket", args.bucket]

        rev_rc = _exec(REVIEWER_PY, reviewer_args)
        if review_out.exists():
            print(f"\n  Relatório de review: {review_out}")
            _print_review_summary(review_out)

        if args.dry_run:
            print("\n  Próximo passo:")
            blocked = _count_verdict(review_out, "BLOCKED") if review_out.exists() else 0
            if blocked:
                print(f"    ⚠️  {blocked} bucket(s) bloqueados — corrija antes de continuar")
                print(f"    Ver detalhes: cat {review_out}")
            else:
                print(f"    python3 s3.py run --env {env} --csv {args.csv} --ticket SEU-TICKET")

    return rc


# ── review ────────────────────────────────────────────────────────────────────

def cmd_review(args: argparse.Namespace) -> int:
    _header("Plan Review")

    plan_files = list(Path(args.output_dir).rglob("plan_output.json"))
    if not plan_files:
        _fail(f"Nenhum plan_output.json em {args.output_dir}")
        print("  Execute primeiro: python3 s3.py run --dry-run ...", file=sys.stderr)
        return 1

    print(f"  Plans encontrados: {len(plan_files)}")
    if args.bucket:
        print(f"  Filtro: {args.bucket}")
    print()

    extra = ["--plan-dir", args.output_dir]
    if args.bucket:
        extra += ["--bucket", args.bucket]
    if args.output:
        extra += ["--output", str(args.output)]
    if args.fail_on_blocked:
        extra += ["--fail-on-blocked"]
    if args.post_to_mr:
        extra += ["--post-to-mr", args.post_to_mr]

    return _exec(REVIEWER_PY, extra)


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    _header(f"Status — {args.output_dir}")

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        _warn(f"Diretório {args.output_dir} não existe — nenhum pipeline executado ainda")
        return 0

    # Conta arquivos gerados
    main_tfs      = list(output_dir.rglob("main.tf"))
    plan_jsons    = list(output_dir.rglob("plan_output.json"))
    import_errors = list(output_dir.rglob("_import_error.txt"))
    plan_errors   = list(output_dir.rglob("_plan_error.txt"))
    changes_mds   = list(output_dir.rglob("CHANGES.md"))

    print(f"  Pasta:            {output_dir.resolve()}")
    print(f"  Buckets gerados:  {len(main_tfs)}  (main.tf)")
    print(f"  Plans executados: {len(plan_jsons)}  (plan_output.json)")
    print(f"  Erros de import:  {len(import_errors)}")
    print(f"  Erros de plan:    {len(plan_errors)}")
    print()

    # Lê plan_report.md se existir
    report_md = output_dir / "plan_report.md"
    if report_md.exists():
        _print_plan_report_summary(report_md)

    # Lê review summary se existir
    review_md = output_dir / "plan_review.md"
    if review_md.exists():
        print(f"  Review disponível: {review_md}")
        _print_review_summary(review_md)

    # Detalhes de erros
    if import_errors:
        print(f"\n  Buckets com import failure:")
        for f in import_errors:
            print(f"    ❌ {_bucket_from_path(f)}")

    if plan_errors:
        print(f"\n  Buckets com plan error:")
        for f in plan_errors:
            print(f"    ❌ {_bucket_from_path(f)}")

    # Sugestão de próximo passo
    print()
    if import_errors or plan_errors:
        print("  Próximo passo: corrija os erros acima e re-execute o run")
    elif plan_jsons and not review_md.exists():
        print("  Próximo passo: python3 s3.py review")
    elif review_md.exists():
        blocked = _count_verdict(review_md, "BLOCKED")
        if blocked:
            print(f"  ⚠️  {blocked} bucket(s) bloqueados no review — corrija antes de abrir MRs")
        else:
            print("  Plans OK — pronto para abrir MRs com: python3 s3.py run ... --ticket SEU-TICKET")

    return 0


# ── Helpers de status ─────────────────────────────────────────────────────────

def _print_plan_report_summary(report_md: Path) -> None:
    try:
        text = report_md.read_text(encoding="utf-8")
        for line in text.split("\n"):
            if "Total:" in line or "Bloqueados:" in line or "Sem MR:" in line:
                print(f"  {line.strip()}")
    except Exception:
        pass


def _print_review_summary(review_md: Path) -> None:
    try:
        text = review_md.read_text(encoding="utf-8")
        for line in text.split("\n"):
            if any(x in line for x in ["🔴", "🟡", "🔵", "✅"]) and "|" in line:
                print(f"  {line.strip()}")
    except Exception:
        pass


def _count_verdict(review_md: Path, verdict: str) -> int:
    try:
        text = review_md.read_text(encoding="utf-8")
        # Procura pela linha da tabela de resumo
        import re
        for line in text.split("\n"):
            if verdict == "BLOCKED" and "Bloqueados" in line and "|" in line:
                m = re.search(r"\|\s*(\d+)\s*\|", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return 0


def _bucket_from_path(p: Path) -> str:
    parts = p.parts
    import re
    try:
        s3_idx = next(i for i, x in enumerate(parts) if x == "s3")
        logical = parts[s3_idx + 1]
        env     = parts[s3_idx + 2]
        repo    = next((x for x in parts if re.match(r"ecs-\w+-default-aws-terraform", x)), "")
        team    = re.match(r"ecs-(\w+)-default", repo).group(1) if repo else ""
        return f"ecs-{team}-{logical}-{env}" if team else f"{logical}-{env}"
    except Exception:
        return p.parent.name


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="s3",
        description="CLI unificado para migração de buckets S3 → Terraform Blueprint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Fluxo recomendado:
  python3 s3.py check
  python3 s3.py discover --env hml --csv levantamento.csv --product lno
  python3 s3.py run --env hml --csv levantamento.csv --product lno --dry-run
  python3 s3.py review
  python3 s3.py run --env hml --csv levantamento.csv --product lno --ticket SRE-1234

Exemplos por produto/ambiente:
  python3 s3.py run --env dev  --csv lev.csv --product ecred --dry-run
  python3 s3.py run --env hml  --csv lev.csv --product lno   --dry-run
  python3 s3.py run --env prd  --csv lev.csv --product lno   --confirm-prd --ticket SRE-1234
        """,
    )
    subs = parser.add_subparsers(dest="command", metavar="comando")
    subs.required = True

    # ── check ────────────────────────────────────────────────────────────────
    subs.add_parser(
        "check",
        help="Valida pré-requisitos (AWS, Terraform, GitLab token, scripts)",
    )

    # ── discover ─────────────────────────────────────────────────────────────
    p_disc = subs.add_parser(
        "discover",
        help="Classifica buckets em BLOCK / REVIEW / AUTO antes de migrar",
    )
    p_disc.add_argument("--csv",        required=True, help="Arquivo CSV do levantamento")
    p_disc.add_argument("--env",        default="dev", choices=["dev", "hml", "prd"],
                        help="Ambiente (padrão: dev)")
    _add_product_arg(p_disc)
    p_disc.add_argument("--output-dir", default="./mr_output", metavar="DIR")
    p_disc.add_argument("--extract",    action="store_true",
                        help="Extrai configs da AWS durante o discovery")
    p_disc.add_argument("--force-extract", action="store_true",
                        help="Re-extrai mesmo se já existir cache")
    p_disc.add_argument("--parallel",   type=int, default=6, metavar="N")

    # ── run ──────────────────────────────────────────────────────────────────
    p_run = subs.add_parser(
        "run",
        help="Pipeline completo: extrai → gera → plan → review → abre MRs",
    )
    p_run.add_argument("--csv",       required=True, help="Arquivo CSV do levantamento")
    p_run.add_argument("--env",       required=True, choices=["dev", "hml", "prd"],
                       help="Ambiente alvo")
    p_run.add_argument("--ticket",    metavar="TICKET",
                       help="Número do ticket (ex: SRE-1234). Obrigatório sem --dry-run")
    _add_product_arg(p_run)
    p_run.add_argument("--bucket",    help="Processar apenas este bucket")
    p_run.add_argument("--dry-run",   action="store_true",
                       help="Gera arquivos e planos localmente. Não abre MRs.")
    p_run.add_argument("--confirm-prd", action="store_true",
                       help="Confirma processamento de PRD (obrigatório para --env prd)")
    p_run.add_argument("--auto-confirm", action="store_true",
                       help="Pula confirmação interativa por bucket (modo lote)")
    p_run.add_argument("--one-mr-per-bucket", action="store_true",
                       help="Abre uma MR separada por bucket (padrão: uma por repo de time)")
    p_run.add_argument("--max-cache-age", type=int, metavar="HORAS",
                       help="Re-extrai configs da AWS se cache tiver mais de N horas "
                            "(recomendado: 24 para HML, 4 para PRD)")
    p_run.add_argument("--exclude",   nargs="+", metavar="BUCKET",
                       help="Excluir estes buckets da abertura de MR (pode passar vários nomes)")
    p_run.add_argument("--parallel",  type=int, default=5, metavar="N",
                       help="Workers paralelos para extração (padrão: 5)")
    p_run.add_argument("--output-dir", default=_OUTPUT_DIR, metavar="DIR")
    p_run.add_argument("--repos-dir",  default=_REPOS_DIR,  metavar="DIR")
    p_run.add_argument("--gitlab-url", default=_GITLAB_URL)
    p_run.add_argument("--gitlab-token")

    # ── review ───────────────────────────────────────────────────────────────
    p_rev = subs.add_parser(
        "review",
        help="Analisa plans existentes e exibe relatório de segurança",
    )
    p_rev.add_argument("--output-dir",    default=_OUTPUT_DIR, metavar="DIR")
    p_rev.add_argument("--bucket",        help="Filtrar por bucket")
    p_rev.add_argument("--output",        type=Path, help="Salvar relatório em arquivo .md")
    p_rev.add_argument("--post-to-mr",    metavar="MR_URL",
                       help="Postar relatório como nota na MR do GitLab")
    p_rev.add_argument("--fail-on-blocked", action="store_true",
                       help="Exit code 1 se houver bloqueados (útil em CI)")

    # ── status ───────────────────────────────────────────────────────────────
    p_st = subs.add_parser(
        "status",
        help="Mostra estado atual do pipeline (processados, bloqueados, pendentes)",
    )
    p_st.add_argument("--output-dir", default=_OUTPUT_DIR, metavar="DIR")

    args = parser.parse_args()

    dispatch = {
        "check":    cmd_check,
        "discover": cmd_discover,
        "run":      cmd_run,
        "review":   cmd_review,
        "status":   cmd_status,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

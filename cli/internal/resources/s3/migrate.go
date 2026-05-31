package s3

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/mulleradriane/migration-cli/internal/resources"
	"github.com/mulleradriane/migration-cli/internal/terraform"
)

// S3Resource implementa a interface resources.Resource para S3.
type S3Resource struct{}

// New retorna uma nova instância de S3Resource.
func New() *S3Resource {
	return &S3Resource{}
}

// Name retorna o identificador do recurso.
func (r *S3Resource) Name() string {
	return "s3"
}

// Discover lê o CSV e classifica cada bucket.
func (r *S3Resource) Discover(ctx context.Context, cfg aws.Config, opts resources.DiscoverOpts) ([]resources.DiscoveryResult, error) {
	return discoverFromCSV(ctx, cfg, opts)
}

// Extract extrai a configuração atual do bucket via AWS APIs.
func (r *S3Resource) Extract(ctx context.Context, cfg aws.Config, bucketName string) (interface{}, error) {
	return ExtractBucketConfig(ctx, cfg, bucketName)
}

// Generate gera os arquivos Terraform a partir da config extraída.
func (r *S3Resource) Generate(ctx context.Context, extracted interface{}, opts resources.MigrateOpts) error {
	bucketCfg, ok := extracted.(*BucketConfig)
	if !ok {
		return fmt.Errorf("tipo inválido: esperado *BucketConfig, recebido %T", extracted)
	}
	return GenerateFiles(bucketCfg, opts.OutputDir)
}

// Migrate orquestra o pipeline completo: preflight → extract → generate → tf init → import → plan
func (r *S3Resource) Migrate(ctx context.Context, awsCfg aws.Config, bucketName string, opts resources.MigrateOpts) error {
	// Determina o diretório de saída
	outputDir := opts.OutputDir
	if outputDir == "" {
		outputDir = filepath.Join(".", bucketName)
	}

	fmt.Printf("\n🪣  Migrando bucket: %s\n", bucketName)
	fmt.Printf("   Time: %s | Env: %s | Category: %s\n", opts.Team, opts.Env, opts.AssetCat)
	fmt.Printf("   Output: %s\n\n", outputDir)

	// === ETAPA 1: Preflight ===
	fmt.Println("━━━ [1/5] Preflight Check ━━━")
	issues, err := Preflight(ctx, awsCfg, bucketName)
	if err != nil {
		return fmt.Errorf("preflight falhou: %w", err)
	}

	blockers := filterBlockers(issues)
	if len(blockers) > 0 {
		fmt.Println("⛔ Bloqueadores encontrados:")
		for _, b := range blockers {
			fmt.Printf("   • [%s] %s\n", b.Code, b.Message)
		}
		return fmt.Errorf("preflight bloqueou a migração: %d bloqueador(es) encontrado(s)", len(blockers))
	}
	fmt.Println("✅ Preflight OK — nenhum bloqueador encontrado")

	// === ETAPA 2: Extract ===
	fmt.Println("\n━━━ [2/5] Extração de Configuração ━━━")
	bucketCfg, err := ExtractBucketConfig(ctx, awsCfg, bucketName)
	if err != nil {
		return fmt.Errorf("extração falhou: %w", err)
	}

	// Enriquece com metadados do opts
	bucketCfg.Team = opts.Team
	bucketCfg.Env = opts.Env
	bucketCfg.AssetCategory = opts.AssetCat

	// Classifica o tier definitivo com base na config real
	tier, tierIssues := ClassifyTierFromConfig(bucketCfg)
	bucketCfg.Tier = tier
	bucketCfg.Issues = append(bucketCfg.Issues, tierIssues...)

	fmt.Printf("✅ Config extraída — Tier: %s\n", tier)

	if tier == TierBLOCK {
		fmt.Println("⛔ Tier BLOCK — salvando config e gerando CHANGES.md com bloqueadores")
		if _, err := SaveBucketConfig(bucketCfg, outputDir); err != nil {
			fmt.Printf("⚠️  Aviso: não foi possível salvar .s3config.json: %v\n", err)
		}
		if err := generateChangesMD(bucketCfg, outputDir); err != nil {
			fmt.Printf("⚠️  Aviso: não foi possível gerar CHANGES.md: %v\n", err)
		}
		return fmt.Errorf("bucket %q está no tier BLOCK — não pode ser migrado automaticamente", bucketName)
	}

	// Salva a config extraída
	configPath, err := SaveBucketConfig(bucketCfg, outputDir)
	if err != nil {
		return fmt.Errorf("falha ao salvar config: %w", err)
	}
	fmt.Printf("   Config salva em: %s\n", configPath)

	// === ETAPA 3: Generate ===
	fmt.Println("\n━━━ [3/5] Geração de Arquivos Terraform ━━━")

	if opts.DryRun {
		fmt.Println("🔍 DRY-RUN: gerando arquivos sem executar Terraform")
	}

	if err := GenerateFiles(bucketCfg, outputDir); err != nil {
		return fmt.Errorf("geração de arquivos falhou: %w", err)
	}

	fmt.Printf("✅ Arquivos gerados em %s/:\n", outputDir)
	fmt.Println("   • main.tf")
	fmt.Println("   • backend.tf")
	fmt.Println("   • CHANGES.md")

	if tier == TierREVIEW {
		fmt.Println("\n⚠️  Tier REVIEW — os seguintes itens requerem atenção:")
		for _, issue := range bucketCfg.Issues {
			if issue.Severity == "review" {
				fmt.Printf("   • [%s] %s\n", issue.Code, issue.Message)
			}
		}
	}

	if opts.DryRun {
		fmt.Println("\n🔍 DRY-RUN: pipeline concluído (Terraform não executado)")
		return nil
	}

	// === ETAPA 4: Terraform Init ===
	fmt.Println("\n━━━ [4/5] Terraform Init ━━━")
	runner := terraform.NewRunner(outputDir)

	if err := runner.Init(ctx); err != nil {
		return fmt.Errorf("terraform init falhou: %w", err)
	}
	fmt.Println("✅ Terraform init concluído")

	// === ETAPA 5a: Terraform Import ===
	if !opts.SkipImport {
		fmt.Println("\n━━━ [5a/5] Terraform Import ━━━")
		importAddr := "module.s3.aws_s3_bucket.this"
		if err := runner.Import(ctx, importAddr, bucketName); err != nil {
			return fmt.Errorf("terraform import falhou: %w", err)
		}
		fmt.Printf("✅ Bucket importado: %s → %s\n", bucketName, importAddr)
	} else {
		fmt.Println("\n⏭️  [5a/5] Terraform Import — pulado (--skip-import)")
	}

	// === ETAPA 5b: Terraform Plan ===
	if !opts.SkipPlan {
		fmt.Println("\n━━━ [5b/5] Terraform Plan ━━━")
		if err := runner.Plan(ctx); err != nil {
			return fmt.Errorf("terraform plan falhou: %w", err)
		}
		fmt.Println("✅ Terraform plan concluído — revise o output acima")
	} else {
		fmt.Println("\n⏭️  [5b/5] Terraform Plan — pulado (--skip-plan)")
	}

	fmt.Printf("\n✅ Migração de %q concluída com sucesso!\n", bucketName)
	if tier == TierREVIEW {
		fmt.Println("   ⚠️  Revise o CHANGES.md e obtenha aprovação antes de aplicar")
	}

	return nil
}

// Preflight verifica bloqueadores no bucket sem gerar nada.
// Equivalente ao s3_preflight.py Python.
func Preflight(ctx context.Context, awsCfg aws.Config, bucketName string) ([]Issue, error) {
	// Extrai apenas as informações necessárias para o preflight
	cfg, err := ExtractBucketConfig(ctx, awsCfg, bucketName)
	if err != nil {
		return nil, fmt.Errorf("falha ao extrair config para preflight: %w", err)
	}

	var issues []Issue

	// Verifica ACL
	if cfg.ACL.CannedACL != "" && cfg.ACL.CannedACL != "private" {
		issues = append(issues, Issue{
			Severity: "block",
			Code:     "ACL_NOT_PRIVATE",
			Message:  fmt.Sprintf("ACL é %q (esperado: private)", cfg.ACL.CannedACL),
		})
	}

	// Verifica KMS key
	if cfg.Encryption.Algorithm == "aws:kms" && cfg.Encryption.KMSKeyID != "" {
		// Não temos team/env aqui, então apenas alertamos
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "KMS_CUSTOM_KEY",
			Message: fmt.Sprintf("KMS key customizada: %s — verifique se é o alias padrão da empresa",
				cfg.Encryption.KMSKeyID),
		})
	}

	// Verifica Object Lock
	if cfg.ObjectLock.Enabled {
		issues = append(issues, Issue{
			Severity: "block",
			Code:     "OBJECT_LOCK_ENABLED",
			Message:  "Object Lock está habilitado — requer revisão manual obrigatória",
		})
	}

	// Verifica Replication
	if cfg.Replication != nil && len(cfg.Replication.Rules) > 0 {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "REPLICATION_ENABLED",
			Message:  fmt.Sprintf("Replicação configurada com %d regra(s)", len(cfg.Replication.Rules)),
		})
	}

	return issues, nil
}

// filterBlockers filtra apenas os issues com severity "block".
func filterBlockers(issues []Issue) []Issue {
	var blockers []Issue
	for _, issue := range issues {
		if issue.Severity == "block" {
			blockers = append(blockers, issue)
		}
	}
	return blockers
}

// formatIssueList formata uma lista de issues para exibição.
func formatIssueList(issues []Issue) string {
	var sb strings.Builder
	for _, issue := range issues {
		icon := "ℹ️"
		switch issue.Severity {
		case "block":
			icon = "⛔"
		case "review":
			icon = "⚠️"
		}
		sb.WriteString(fmt.Sprintf("  %s [%s] %s\n", icon, issue.Code, issue.Message))
	}
	return sb.String()
}

// MigrateWave executa a migração de múltiplos buckets em sequência (uma onda).
func MigrateWave(ctx context.Context, awsCfg aws.Config, buckets []string, opts resources.MigrateOpts) (int, int, error) {
	succeeded := 0
	failed := 0

	resource := New()

	for _, bucket := range buckets {
		bucketOpts := opts
		if bucketOpts.OutputDir == "" {
			bucketOpts.OutputDir = filepath.Join(".", "output", bucket)
		}

		if err := resource.Migrate(ctx, awsCfg, bucket, bucketOpts); err != nil {
			fmt.Printf("\n❌ Bucket %q falhou: %v\n", bucket, err)
			failed++
			continue
		}
		succeeded++
	}

	return succeeded, failed, nil
}

// formatIssueList is exported for use in cmd layer
var _ = formatIssueList

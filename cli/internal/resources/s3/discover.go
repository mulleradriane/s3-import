package s3

import (
	"context"
	"encoding/csv"
	"fmt"
	"os"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/mulleradriane/migration-cli/internal/resources"
)

// discoverFromCSV lê o CSV de levantamento e classifica cada bucket em um tier.
// O CSV deve ter cabeçalho: bucket_name,team,env,asset_category,category,blockers
func discoverFromCSV(ctx context.Context, cfg aws.Config, opts resources.DiscoverOpts) ([]resources.DiscoveryResult, error) {
	f, err := os.Open(opts.CSVPath)
	if err != nil {
		return nil, fmt.Errorf("não foi possível abrir CSV %q: %w", opts.CSVPath, err)
	}
	defer f.Close()

	reader := csv.NewReader(f)
	reader.TrimLeadingSpace = true
	reader.Comment = '#'

	records, err := reader.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("erro ao ler CSV: %w", err)
	}

	if len(records) == 0 {
		return nil, fmt.Errorf("CSV vazio: %q", opts.CSVPath)
	}

	// Mapeia o cabeçalho para índices de coluna de forma tolerante.
	header := records[0]
	colIdx := parseHeader(header)
	if err := validateHeader(colIdx); err != nil {
		return nil, err
	}

	var results []resources.DiscoveryResult

	for i, row := range records[1:] {
		if len(row) == 0 || (len(row) == 1 && row[0] == "") {
			continue // ignora linhas em branco
		}

		rec, err := parseRow(colIdx, row, i+2)
		if err != nil {
			return nil, err
		}

		// Aplica filtros opcionais
		if opts.Prefix != "" && !strings.HasPrefix(rec.BucketName, opts.Prefix) {
			continue
		}
		if opts.Env != "" && !strings.EqualFold(rec.Env, opts.Env) {
			continue
		}

		tier, reason := classifyTier(rec)

		extra := map[string]string{
			"team":           rec.Team,
			"env":            rec.Env,
			"asset_category": rec.AssetCategory,
			"category":       rec.Category,
			"blockers":       rec.Blockers,
		}

		results = append(results, resources.DiscoveryResult{
			Name:   rec.BucketName,
			Tier:   string(tier),
			Reason: reason,
			Extra:  extra,
		})
	}

	return results, nil
}

// parseHeader mapeia nomes de coluna para índices, com tolerância a variações de capitalização.
func parseHeader(header []string) map[string]int {
	idx := make(map[string]int)
	for i, h := range header {
		normalized := strings.ToLower(strings.TrimSpace(h))
		// Normaliza aliases comuns
		switch normalized {
		case "bucket_name", "bucket", "name":
			idx["bucket_name"] = i
		case "team", "time":
			idx["team"] = i
		case "env", "environment", "ambiente":
			idx["env"] = i
		case "asset_category", "asset category", "categoria_asset":
			idx["asset_category"] = i
		case "category", "categoria":
			idx["category"] = i
		case "blockers", "bloqueadores", "blocker":
			idx["blockers"] = i
		default:
			idx[normalized] = i
		}
	}
	return idx
}

// validateHeader verifica que o CSV tem as colunas obrigatórias.
func validateHeader(colIdx map[string]int) error {
	required := []string{"bucket_name", "team", "env", "asset_category"}
	var missing []string
	for _, col := range required {
		if _, ok := colIdx[col]; !ok {
			missing = append(missing, col)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("CSV sem colunas obrigatórias: %s", strings.Join(missing, ", "))
	}
	return nil
}

// parseRow extrai um CSVRecord de uma linha do CSV.
func parseRow(colIdx map[string]int, row []string, lineNum int) (CSVRecord, error) {
	get := func(key string) string {
		idx, ok := colIdx[key]
		if !ok || idx >= len(row) {
			return ""
		}
		return strings.TrimSpace(row[idx])
	}

	rec := CSVRecord{
		BucketName:    get("bucket_name"),
		Team:          get("team"),
		Env:           get("env"),
		AssetCategory: get("asset_category"),
		Category:      get("category"),
		Blockers:      get("blockers"),
	}

	if rec.BucketName == "" {
		return CSVRecord{}, fmt.Errorf("linha %d: bucket_name vazio", lineNum)
	}

	return rec, nil
}

// classifyTier classifica um bucket em BLOCK, REVIEW ou AUTO baseado nos dados do CSV.
// Esta é a lógica de tier da fase de discovery (sem chamar AWS).
// A classificação completa (com dados reais do bucket) é feita em extract + migrate.
func classifyTier(rec CSVRecord) (Tier, string) {
	cat := rec.AssetCategory

	// Tenta normalizar categoria legada/com typo antes de validar
	if canonical, normalized := NormalizeAssetCategory(cat); normalized {
		return TierREVIEW, fmt.Sprintf(
			"asset_category normalizada: %q → %q (será aplicada no main.tf — revise CHANGES.md)",
			cat, canonical,
		)
	}

	// BLOCK: asset_category ainda inválida após tentativa de normalização
	if !ValidAssetCategories[cat] {
		return TierBLOCK, fmt.Sprintf(
			"asset_category inválida: %q (válidas: Productive data, Code, Logs, Cache, Backup, Temporary data, Configuration)",
			cat,
		)
	}

	// BLOCK: tem bloqueadores no CSV
	if strings.TrimSpace(rec.Blockers) != "" {
		return TierBLOCK, fmt.Sprintf("bloqueadores encontrados: %s", rec.Blockers)
	}

	// AUTO: nenhum bloqueador CSV → tier provisório (pode mudar após extract)
	return TierAUTO, "nenhum bloqueador identificado no CSV (classificação provisória)"
}

// ClassifyTierFromConfig classifica o tier de um bucket baseado na config real extraída da AWS.
// Esta é a classificação definitiva, executada após o extract.
func ClassifyTierFromConfig(cfg *BucketConfig) (Tier, []Issue) {
	var issues []Issue

	// --- BLOCK conditions ---

	// Normaliza a categoria antes de validar
	if canonical, normalized := NormalizeAssetCategory(cfg.AssetCategory); normalized {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "ASSET_CATEGORY_NORMALIZED",
			Message: fmt.Sprintf("asset_category normalizada: %q → %q — verifique se a categoria correta foi aplicada",
				cfg.AssetCategory, canonical),
		})
		cfg.AssetCategory = canonical // aplica a normalização para o generate
	} else if !ValidAssetCategories[cfg.AssetCategory] {
		issues = append(issues, Issue{
			Severity: "block",
			Code:     "INVALID_ASSET_CATEGORY",
			Message:  fmt.Sprintf("asset_category inválida: %q", cfg.AssetCategory),
		})
	}

	// Bloqueadores no CSV
	if strings.TrimSpace(cfg.Blockers) != "" {
		issues = append(issues, Issue{
			Severity: "block",
			Code:     "CSV_BLOCKERS",
			Message:  fmt.Sprintf("bloqueadores declarados no CSV: %s", cfg.Blockers),
		})
	}

	// KMS key não é o alias padrão da empresa
	if cfg.Encryption.Algorithm == "aws:kms" && cfg.Encryption.KMSKeyID != "" {
		expectedAlias := ExpectedKMSAlias(cfg.Team, cfg.Env)
		// KMS pode vir como ARN ou alias; verificamos se contém o alias esperado
		if !strings.Contains(cfg.Encryption.KMSKeyID, expectedAlias) &&
			!strings.Contains(cfg.Encryption.KMSKeyID, cfg.Team+"-default-"+cfg.Env) {
			issues = append(issues, Issue{
				Severity: "block",
				Code:     "KMS_KEY_NOT_DEFAULT",
				Message: fmt.Sprintf("KMS key %q não é o alias padrão da empresa (%s)",
					cfg.Encryption.KMSKeyID, expectedAlias),
			})
		}
	}

	// ACL não é private
	if cfg.ACL.CannedACL != "" && cfg.ACL.CannedACL != "private" {
		issues = append(issues, Issue{
			Severity: "block",
			Code:     "ACL_NOT_PRIVATE",
			Message:  fmt.Sprintf("ACL do bucket é %q (esperado: private)", cfg.ACL.CannedACL),
		})
	}

	// Se há qualquer BLOCK, retorna imediatamente
	for _, issue := range issues {
		if issue.Severity == "block" {
			return TierBLOCK, issues
		}
	}

	// --- REVIEW conditions ---

	// AES256 configurado explicitamente (desde abr/2023 a AWS aplica por padrão)
	if cfg.Encryption.Enabled && cfg.Encryption.Algorithm == "AES256" {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "AES256_EXPLICIT",
			Message:  "AES256 configurado explicitamente (desnecessário desde abr/2023, AWS aplica por padrão)",
		})
	}

	// public_block_acl ativo
	if cfg.PublicAccessBlock.BlockPublicAcls {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "PUBLIC_BLOCK_ACL",
			Message:  "BlockPublicAcls ativo — será mantido sem alteração, informado no CHANGES.md",
		})
	}

	// Object Lock ativo
	if cfg.ObjectLock.Enabled {
		issues = append(issues, Issue{
			Severity: "block",
			Code:     "OBJECT_LOCK",
			Message:  "Object Lock ativo — requer revisão manual antes da migração",
		})
		return TierBLOCK, issues
	}

	// Replicação configurada
	if cfg.Replication != nil && len(cfg.Replication.Rules) > 0 {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "REPLICATION_CONFIGURED",
			Message:  "Replicação configurada — será mantida sem alteração, documentada no CHANGES.md",
		})
	}

	// Lifecycle custom
	if len(cfg.Lifecycle) > 0 {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "LIFECYCLE_CUSTOM",
			Message:  "Lifecycle custom encontrado — será padronizado para o padrão BP (economiza storage $$)",
		})
	}

	// Versioning habilitado
	if cfg.Versioning.Status == "Enabled" {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "VERSIONING_ENABLED",
			Message:  "Versioning habilitado — será mantido, documentado no CHANGES.md",
		})
	}

	// Website configurado
	if cfg.Website != nil {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "WEBSITE_CONFIGURED",
			Message:  "Website estático configurado — será mantido sem alteração, documentado no CHANGES.md",
		})
	}

	// CORS configurado
	if len(cfg.CORS) > 0 {
		issues = append(issues, Issue{
			Severity: "review",
			Code:     "CORS_CONFIGURED",
			Message:  "CORS configurado — será mantido sem alteração, documentado no CHANGES.md",
		})
	}

	// Logging para bucket não-padrão
	if cfg.Logging.TargetBucket != "" {
		expectedLogBucket := cfg.Team + "-" + cfg.Env + "-logs"
		if cfg.Logging.TargetBucket != expectedLogBucket {
			issues = append(issues, Issue{
				Severity: "review",
				Code:     "LOGGING_NONSTANDARD_TARGET",
				Message: fmt.Sprintf("Logging para bucket não-padrão %q (esperado: %q)",
					cfg.Logging.TargetBucket, expectedLogBucket),
			})
		}
	}

	// Se há REVIEW issues → REVIEW
	for _, issue := range issues {
		if issue.Severity == "review" {
			return TierREVIEW, issues
		}
	}

	return TierAUTO, issues
}

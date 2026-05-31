package s3

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"text/template"
	"time"
)

// GenerateFiles gera os arquivos Terraform (main.tf, backend.tf) e CHANGES.md
// para um bucket S3, baseado na configuração extraída.
func GenerateFiles(cfg *BucketConfig, outputDir string) error {
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		return fmt.Errorf("falha ao criar diretório %q: %w", outputDir, err)
	}

	if err := generateBackendTF(cfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar backend.tf: %w", err)
	}

	if err := generateMainTF(cfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar main.tf: %w", err)
	}

	if err := generateChangesMD(cfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar CHANGES.md: %w", err)
	}

	return nil
}

// generateBackendTF gera o arquivo backend.tf com o padrão BP.
func generateBackendTF(cfg *BucketConfig, outputDir string) error {
	const tmplText = `terraform {
  backend "s3" {
    bucket = "{{ .BackendBucket }}"
    key    = "{{ .BackendKey }}"
    region = "us-east-1"
  }
}
`
	tmpl, err := template.New("backend").Parse(tmplText)
	if err != nil {
		return err
	}

	data := struct {
		BackendBucket string
		BackendKey    string
	}{
		BackendBucket: cfg.BackendBucket(),
		BackendKey:    cfg.BackendKey(),
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, data); err != nil {
		return err
	}

	return os.WriteFile(filepath.Join(outputDir, "backend.tf"), buf.Bytes(), 0644)
}

// generateMainTF gera o arquivo main.tf com o módulo Blueprint S3.
func generateMainTF(cfg *BucketConfig, outputDir string) error {
	const tmplText = `# Gerado automaticamente pelo migration-cli em {{ .GeneratedAt }}
# Bucket: {{ .BucketName }}
# Time:   {{ .Team }}
# Env:    {{ .Env }}
# Tier:   {{ .Tier }}
# ⚠️  NÃO edite manualmente — use: migration-cli s3 generate --bucket {{ .BucketName }}

module "s3" {
  source = "git::https://gitlab.com/company/tf-modules/bp-s3.git?ref=v2.0.0"

  bucket_name    = "{{ .BucketName }}"
  team           = "{{ .Team }}"
  environment    = "{{ .Env }}"
  asset_category = "{{ .AssetCategory }}"
{{ if .LifecycleRulesHCL }}
  lifecycle_rules = [
{{ .LifecycleRulesHCL }}  ]
{{ end }}{{ if .IgnoreChanges }}
  lifecycle {
    ignore_changes = [
{{ .IgnoreChanges }}    ]
  }
{{ end }}}
`
	funcMap := template.FuncMap{
		"indent": func(n int, s string) string {
			pad := strings.Repeat("  ", n)
			return pad + strings.ReplaceAll(s, "\n", "\n"+pad)
		},
	}

	tmpl, err := template.New("main").Funcs(funcMap).Parse(tmplText)
	if err != nil {
		return err
	}

	data := struct {
		GeneratedAt       string
		BucketName        string
		Team              string
		Env               string
		Tier              string
		AssetCategory     string
		LifecycleRulesHCL string
		IgnoreChanges     string
	}{
		GeneratedAt:       time.Now().Format("2006-01-02 15:04:05 MST"),
		BucketName:        cfg.BucketName,
		Team:              cfg.Team,
		Env:               cfg.Env,
		Tier:              string(cfg.Tier),
		AssetCategory:     cfg.AssetCategory,
		LifecycleRulesHCL: buildLifecycleRulesHCL(cfg.Lifecycle),
		IgnoreChanges:     buildIgnoreChanges(cfg),
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, data); err != nil {
		return err
	}

	return os.WriteFile(filepath.Join(outputDir, "main.tf"), buf.Bytes(), 0644)
}

// buildLifecycleRulesHCL converte as regras de lifecycle para HCL.
func buildLifecycleRulesHCL(rules []LifecycleRule) string {
	if len(rules) == 0 {
		return ""
	}

	var sb strings.Builder
	for _, rule := range rules {
		sb.WriteString("    {\n")
		sb.WriteString(fmt.Sprintf("      id     = %q\n", rule.ID))
		sb.WriteString(fmt.Sprintf("      status = %q\n", rule.Status))

		if rule.Filter.Prefix != "" {
			sb.WriteString(fmt.Sprintf("      prefix = %q\n", rule.Filter.Prefix))
		}

		for _, t := range rule.Transitions {
			sb.WriteString("      transition {\n")
			sb.WriteString(fmt.Sprintf("        days          = %d\n", t.Days))
			sb.WriteString(fmt.Sprintf("        storage_class = %q\n", t.StorageClass))
			sb.WriteString("      }\n")
		}

		if rule.Expiration != nil {
			sb.WriteString("      expiration {\n")
			if rule.Expiration.Days > 0 {
				sb.WriteString(fmt.Sprintf("        days = %d\n", rule.Expiration.Days))
			}
			if rule.Expiration.Date != nil {
				sb.WriteString(fmt.Sprintf("        date = %q\n", rule.Expiration.Date.Format("2006-01-02")))
			}
			sb.WriteString("      }\n")
		}

		if rule.NoncurrentVersion != nil {
			sb.WriteString("      noncurrent_version_expiration {\n")
			sb.WriteString(fmt.Sprintf("        days = %d\n", rule.NoncurrentVersion.Days))
			sb.WriteString("      }\n")
		}

		sb.WriteString("    },\n")
	}

	return sb.String()
}

// buildIgnoreChanges gera o bloco ignore_changes para itens REVIEW.
func buildIgnoreChanges(cfg *BucketConfig) string {
	var changes []string

	for _, issue := range cfg.Issues {
		if issue.Severity != "review" {
			continue
		}
		switch issue.Code {
		case "AES256_EXPLICIT":
			changes = append(changes, "      server_side_encryption_configuration,")
		case "CORS_CONFIGURED":
			changes = append(changes, "      cors_rule,")
		case "VERSIONING_ENABLED":
			changes = append(changes, "      versioning,")
		case "WEBSITE_CONFIGURED":
			changes = append(changes, "      website,")
		case "REPLICATION_CONFIGURED":
			changes = append(changes, "      replication_configuration,")
		case "LOGGING_NONSTANDARD_TARGET":
			changes = append(changes, "      logging,")
		}
	}

	if len(changes) == 0 {
		return ""
	}

	return strings.Join(changes, "\n") + "\n"
}

// generateChangesMD gera o arquivo CHANGES.md documentando o que foi encontrado.
func generateChangesMD(cfg *BucketConfig, outputDir string) error {
	const tmplText = `# CHANGES.md — Migração S3 para Blueprint Terraform

**Bucket:** ` + "`{{ .BucketName }}`" + `
**Time:** {{ .Team }}
**Ambiente:** {{ .Env }}
**Asset Category:** {{ .AssetCategory }}
**Tier:** {{ .Tier }}
**Gerado em:** {{ .GeneratedAt }}

---

## Resumo da migração

{{ if eq .Tier "BLOCK" -}}
> ⛔ **BLOQUEADO** — Este bucket não pode ser migrado automaticamente.
> Resolva os bloqueadores abaixo antes de prosseguir.
{{ else if eq .Tier "REVIEW" -}}
> ⚠️ **REVISÃO NECESSÁRIA** — Este bucket foi gerado mas requer revisão manual
> dos itens listados abaixo antes de aplicar o Terraform.
{{ else -}}
> ✅ **AUTO** — Este bucket pode ser migrado automaticamente sem revisão manual.
{{ end }}
---

## O que foi encontrado

{{ if .HasEncryption -}}
### Criptografia
- **Algoritmo:** {{ .EncryptionAlg }}{{ if .KMSKey }} (KMS Key: ` + "`{{ .KMSKey }}`" + `){{ end }}
- **Status:** Mantido sem alteração — a AWS aplica AES256 por padrão desde abril/2023.

{{ end -}}
{{ if .HasLifecycle -}}
### Lifecycle Rules ({{ .LifecycleCount }} regras encontradas)
As regras de lifecycle abaixo **serão padronizadas** para o padrão BP.
Isso pode resultar em **economia de custos** com transições de storage tier.

{{ range .LifecycleRules -}}
- **{{ .ID }}** ({{ .Status }}): {{ .Description }}
{{ end }}
{{ end -}}
{{ if .HasVersioning -}}
### Versionamento
- **Status:** {{ .VersioningStatus }}
- Será mantido sem alteração. Documentado para controle.

{{ end -}}
{{ if .HasCORS -}}
### CORS ({{ .CORSCount }} regra(s))
- **Status:** Mantido sem alteração.
- Configure no módulo BP via variável ` + "`cors_rules`" + ` se necessário.

{{ end -}}
{{ if .HasPolicy -}}
### Bucket Policy
- **Status:** Mantida sem alteração — políticas são gerenciadas pelo time de segurança.
- Verifique se a policy é compatível com o módulo BP.

{{ end -}}
{{ if .HasWebsite -}}
### Website Estático
- **Index:** {{ .WebsiteIndex }}
- **Error:** {{ .WebsiteError }}
- **Status:** Mantido sem alteração.

{{ end -}}
{{ if .HasLogging -}}
### Access Logging
- **Target Bucket:** {{ .LoggingTarget }}
- **Prefix:** {{ .LoggingPrefix }}
- **Status:** Mantido sem alteração.

{{ end -}}
{{ if .HasReplication -}}
### Replicação ({{ .ReplicationRules }} regra(s))
- **Role:** ` + "`{{ .ReplicationRole }}`" + `
- **Status:** Mantida sem alteração — replicação é crítica para DR.

{{ end -}}
{{ if .HasObjectLock -}}
### Object Lock ⛔
- **Modo:** {{ .ObjectLockMode }}
- **Retenção:** {{ .ObjectLockRetention }}
- **Status:** BLOQUEADOR — Object Lock requer revisão manual obrigatória.

{{ end -}}

---

## Itens que requerem revisão manual

{{ if .ReviewIssues -}}
{{ range .ReviewIssues -}}
### ⚠️ {{ .Code }}
{{ .Message }}

{{ end -}}
{{ else -}}
Nenhum item requer revisão manual. ✅

{{ end -}}

---

## Itens bloqueadores

{{ if .BlockIssues -}}
{{ range .BlockIssues -}}
### ⛔ {{ .Code }}
{{ .Message }}

{{ end -}}
{{ else -}}
Nenhum bloqueador encontrado. ✅

{{ end -}}

---

## Próximos passos

{{ if eq .Tier "AUTO" -}}
1. Revisar o ` + "`main.tf`" + ` gerado
2. Executar: ` + "`terraform init`" + `
3. Executar: ` + "`terraform import module.s3.aws_s3_bucket.this {{ .BucketName }}`" + `
4. Executar: ` + "`terraform plan`" + ` — verificar que não há mudanças destrutivas
5. Executar: ` + "`terraform apply`" + ` (se plano aprovado)
{{ else if eq .Tier "REVIEW" -}}
1. Revisar os itens marcados como ⚠️ acima
2. Ajustar o ` + "`main.tf`" + ` conforme necessário
3. Executar: ` + "`terraform init`" + `
4. Executar: ` + "`terraform import module.s3.aws_s3_bucket.this {{ .BucketName }}`" + `
5. Executar: ` + "`terraform plan`" + ` — verificar cuidadosamente
6. Obter aprovação do time antes de aplicar
{{ else -}}
1. Resolver os bloqueadores listados acima
2. Re-executar: ` + "`migration-cli s3 migrate --bucket {{ .BucketName }}`" + `
{{ end }}

---
*Gerado automaticamente por migration-cli v1.0.0*
`

	// Prepara dados derivados para o template
	type lifecycleSummary struct {
		ID          string
		Status      string
		Description string
	}

	var lifecycleSummaries []lifecycleSummary
	for _, rule := range cfg.Lifecycle {
		desc := ""
		if len(rule.Transitions) > 0 {
			var parts []string
			for _, t := range rule.Transitions {
				parts = append(parts, fmt.Sprintf("%d dias → %s", t.Days, t.StorageClass))
			}
			desc = "Transitions: " + strings.Join(parts, ", ")
		}
		if rule.Expiration != nil {
			if rule.Expiration.Days > 0 {
				desc += fmt.Sprintf("; Expira em %d dias", rule.Expiration.Days)
			}
		}
		if desc == "" {
			desc = "regra customizada"
		}
		lifecycleSummaries = append(lifecycleSummaries, lifecycleSummary{
			ID:          rule.ID,
			Status:      rule.Status,
			Description: desc,
		})
	}

	var reviewIssues, blockIssues []Issue
	for _, issue := range cfg.Issues {
		switch issue.Severity {
		case "review":
			reviewIssues = append(reviewIssues, issue)
		case "block":
			blockIssues = append(blockIssues, issue)
		}
	}

	objectLockRetention := ""
	if cfg.ObjectLock.RetentionDays > 0 {
		objectLockRetention = fmt.Sprintf("%d dias", cfg.ObjectLock.RetentionDays)
	} else if cfg.ObjectLock.RetentionYears > 0 {
		objectLockRetention = fmt.Sprintf("%d anos", cfg.ObjectLock.RetentionYears)
	}

	replicationRules := 0
	replicationRole := ""
	if cfg.Replication != nil {
		replicationRules = len(cfg.Replication.Rules)
		replicationRole = cfg.Replication.Role
	}

	webIndex, webError := "", ""
	if cfg.Website != nil {
		webIndex = cfg.Website.IndexDocument
		webError = cfg.Website.ErrorDocument
	}

	data := struct {
		GeneratedAt        string
		BucketName         string
		Team               string
		Env                string
		AssetCategory      string
		Tier               string
		HasEncryption      bool
		EncryptionAlg      string
		KMSKey             string
		HasLifecycle       bool
		LifecycleCount     int
		LifecycleRules     []lifecycleSummary
		HasVersioning      bool
		VersioningStatus   string
		HasCORS            bool
		CORSCount          int
		HasPolicy          bool
		HasWebsite         bool
		WebsiteIndex       string
		WebsiteError       string
		HasLogging         bool
		LoggingTarget      string
		LoggingPrefix      string
		HasReplication     bool
		ReplicationRules   int
		ReplicationRole    string
		HasObjectLock      bool
		ObjectLockMode     string
		ObjectLockRetention string
		ReviewIssues       []Issue
		BlockIssues        []Issue
	}{
		GeneratedAt:        time.Now().Format("2006-01-02 15:04:05"),
		BucketName:         cfg.BucketName,
		Team:               cfg.Team,
		Env:                cfg.Env,
		AssetCategory:      cfg.AssetCategory,
		Tier:               string(cfg.Tier),
		HasEncryption:      cfg.Encryption.Enabled,
		EncryptionAlg:      cfg.Encryption.Algorithm,
		KMSKey:             cfg.Encryption.KMSKeyID,
		HasLifecycle:       len(cfg.Lifecycle) > 0,
		LifecycleCount:     len(cfg.Lifecycle),
		LifecycleRules:     lifecycleSummaries,
		HasVersioning:      cfg.Versioning.Status != "",
		VersioningStatus:   cfg.Versioning.Status,
		HasCORS:            len(cfg.CORS) > 0,
		CORSCount:          len(cfg.CORS),
		HasPolicy:          cfg.Policy != "",
		HasWebsite:         cfg.Website != nil,
		WebsiteIndex:       webIndex,
		WebsiteError:       webError,
		HasLogging:         cfg.Logging.TargetBucket != "",
		LoggingTarget:      cfg.Logging.TargetBucket,
		LoggingPrefix:      cfg.Logging.TargetPrefix,
		HasReplication:     cfg.Replication != nil && len(cfg.Replication.Rules) > 0,
		ReplicationRules:   replicationRules,
		ReplicationRole:    replicationRole,
		HasObjectLock:      cfg.ObjectLock.Enabled,
		ObjectLockMode:     cfg.ObjectLock.Mode,
		ObjectLockRetention: objectLockRetention,
		ReviewIssues:       reviewIssues,
		BlockIssues:        blockIssues,
	}

	tmpl, err := template.New("changes").Parse(tmplText)
	if err != nil {
		return fmt.Errorf("falha ao parsear template CHANGES.md: %w", err)
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, data); err != nil {
		return fmt.Errorf("falha ao executar template CHANGES.md: %w", err)
	}

	return os.WriteFile(filepath.Join(outputDir, "CHANGES.md"), buf.Bytes(), 0644)
}

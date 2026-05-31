package apigw

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"text/template"
	"time"
)

// GenerateFiles gera os arquivos Terraform e documentação para uma API Gateway.
// Gera: openapi.json, main.tf, backend.tf, CHANGES.md, _import_commands.sh
func GenerateFiles(apicfg *APIConfig, outputDir string) error {
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		return fmt.Errorf("falha ao criar diretório %q: %w", outputDir, err)
	}

	// Copia openapi.json se disponível
	if len(apicfg.OAS3Export) > 0 {
		if err := os.WriteFile(filepath.Join(outputDir, "openapi.json"), apicfg.OAS3Export, 0644); err != nil {
			return fmt.Errorf("falha ao escrever openapi.json: %w", err)
		}
	}

	// Gera policy.json para PRIVATE APIs
	if apicfg.EndpointType == "PRIVATE" && apicfg.Policy != "" {
		if err := os.WriteFile(filepath.Join(outputDir, "policy.json"), []byte(apicfg.Policy), 0644); err != nil {
			return fmt.Errorf("falha ao escrever policy.json: %w", err)
		}
	}

	if err := generateBackendTF(apicfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar backend.tf: %w", err)
	}

	if err := generateMainTF(apicfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar main.tf: %w", err)
	}

	if err := generateChangesMD(apicfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar CHANGES.md: %w", err)
	}

	if err := generateImportScript(apicfg, outputDir); err != nil {
		return fmt.Errorf("falha ao gerar _import_commands.sh: %w", err)
	}

	return nil
}

// generateBackendTF gera o arquivo backend.tf.
func generateBackendTF(apicfg *APIConfig, outputDir string) error {
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
		BackendBucket: apicfg.BackendBucket(),
		BackendKey:    apicfg.BackendKey(),
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, data); err != nil {
		return err
	}

	return os.WriteFile(filepath.Join(outputDir, "backend.tf"), buf.Bytes(), 0644)
}

// generateMainTF gera o arquivo main.tf com o módulo Blueprint API Gateway.
func generateMainTF(apicfg *APIConfig, outputDir string) error {
	// Determina o nome do módulo (product ou nome original para legados)
	moduleName := apicfg.Product
	if moduleName == "" {
		moduleName = apicfg.Name
	}

	// Determina o client_certificate_id
	certID := "null"
	for _, s := range apicfg.Stages {
		if s.ClientCertificateID != "" {
			certID = fmt.Sprintf("%q", s.ClientCertificateID)
			break
		}
	}

	// Monta comentário de stages para Pattern A
	var stagesComment string
	if apicfg.Pattern == PatternA && len(apicfg.Stages) > 1 {
		var names []string
		for _, s := range apicfg.Stages {
			names = append(names, s.Name)
		}
		stagesComment = fmt.Sprintf("\n  # Pattern A: API multi-stage — stages encontrados: %s", strings.Join(names, ", "))
	}

	// Monta comentário de legacy_name
	var legacyComment string
	if apicfg.LegacyName {
		legacyComment = fmt.Sprintf("\n  legacy_name = %q # nome original não segue padrão apigw-{product}", apicfg.Name)
	}

	// Monta bloco de policy para PRIVATE
	var policyBlock string
	if apicfg.EndpointType == "PRIVATE" {
		policyBlock = "\n  # apigateway_policy = file(\"${path.module}/policy.json\")  # descomente se PRIVATE após validar"
	}

	// Monta o environment
	envValue := ""
	if apicfg.Pattern == PatternB && apicfg.Env != "" {
		envValue = fmt.Sprintf("\n  environment   = %q", apicfg.Env)
	}

	const tmplText = `# Gerado automaticamente pelo migration-cli em {{ .GeneratedAt }}
# API: {{ .Name }} ({{ .APIID }})
# Tier: {{ .Tier }}
# AVISO: Não edite manualmente — use: migration-cli apigw generate --api-id {{ .APIID }}
{{ .StagesComment }}
module "apigw" {
  source = "git::https://gitlab.com/company/tf-modules/bp-apigw.git?ref=main"

  name          = {{ .ModuleName | printf "%q" }}
  team          = {{ .Team | printf "%q" }}{{ .EnvValue }}
  endpoint_type = {{ .EndpointType | printf "%q" }}{{ .LegacyComment }}

  body = templatefile("${path.module}/openapi.json", {})

  # Client certificate (null se API não tem certificado)
  client_certificate_id = {{ .CertID }}
{{ .PolicyBlock }}
  lifecycle {
    ignore_changes = [
      # Campos gerenciados fora do Terraform por enquanto:
      # deployment_id,  # deployment trigger será ativado após BP estabilizar
    ]
  }
}
`
	tmpl, err := template.New("main").Parse(tmplText)
	if err != nil {
		return err
	}

	data := struct {
		GeneratedAt   string
		Name          string
		APIID         string
		Tier          string
		ModuleName    string
		Team          string
		EnvValue      string
		EndpointType  string
		CertID        string
		StagesComment string
		LegacyComment string
		PolicyBlock   string
	}{
		GeneratedAt:   time.Now().Format("2006-01-02 15:04:05 MST"),
		Name:          apicfg.Name,
		APIID:         apicfg.ID,
		Tier:          string(apicfg.Tier),
		ModuleName:    moduleName,
		Team:          apicfg.Team,
		EnvValue:      envValue,
		EndpointType:  apicfg.EndpointType,
		CertID:        certID,
		StagesComment: stagesComment,
		LegacyComment: legacyComment,
		PolicyBlock:   policyBlock,
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, data); err != nil {
		return err
	}

	return os.WriteFile(filepath.Join(outputDir, "main.tf"), buf.Bytes(), 0644)
}

// generateChangesMD gera o arquivo CHANGES.md.
func generateChangesMD(apicfg *APIConfig, outputDir string) error {
	const tmplText = `# CHANGES.md — Migração API Gateway para Blueprint Terraform

**API:** ` + "`{{ .Name }}`" + ` (ID: {{ .APIID }})
**Produto:** {{ .Product }}
**Time:** {{ .Team }}
**Ambiente:** {{ if .Env }}{{ .Env }}{{ else }}todos os stages (Pattern A){{ end }}
**Endpoint Type:** {{ .EndpointType }}
**Pattern:** {{ .Pattern }}
**Tier:** {{ .Tier }}
**Stages:** {{ .StageList }}
**Gerado em:** {{ .GeneratedAt }}

---

## Resumo da migração

{{ if eq .Tier "BLOCK" -}}
> BLOQUEADO — Esta API não pode ser migrada automaticamente.
> Resolva os bloqueadores abaixo antes de prosseguir.
{{ else if eq .Tier "REVIEW_VPCLINK" -}}
> REVISÃO NECESSÁRIA — API usa VPC Link. Verifique o naming e configuração antes de importar.
{{ else if eq .Tier "REVIEW_HARDCODED" -}}
> REVISÃO NECESSÁRIA — URI de integração contém ambiente hardcoded. Use stage variables.
{{ else if eq .Tier "REVIEW_LIMIT" -}}
> REVISÃO NECESSÁRIA — API está próxima do limite de 500 resources.
{{ else -}}
> AUTO — Esta API pode ser migrada automaticamente sem revisão manual.
{{ end }}
---

## O que foi encontrado

### Configuração da API
- **Endpoint Type:** {{ .EndpointType }}
- **Pattern:** {{ .Pattern }}{{ if eq .Pattern "A" }} (1 API para todos os envs){{ else }} (1 API por env){{ end }}
- **Stages:** {{ .StageList }}
- **Resource count:** {{ .ResourceCount }}
{{ if .HasLegacyName -}}
- **Nome legacy:** ` + "`{{ .Name }}`" + ` (não segue padrão apigw-{product})
{{ end -}}
{{ if .HasAuthorizers -}}

### Authorizers ({{ .AuthorizerCount }} encontrado(s))
Os authorizers abaixo **não são gerenciados** pelo módulo BP neste momento:
{{ range .Authorizers -}}
- **{{ .Name }}** ({{ .Type }}): ` + "`{{ .ID }}`" + `
{{ end -}}
{{ end -}}
{{ if .HasUsagePlans -}}

### Usage Plans ({{ .UsagePlanCount }} encontrado(s))
Os usage plans abaixo **não são gerenciados** pelo módulo BP neste momento:
{{ range .UsagePlans -}}
- **{{ .Name }}** (ID: {{ .ID }}){{ if .Quota }}: limite={{ .Quota.Limit }}/{{ .Quota.Period }}{{ end }}
{{ end -}}
{{ end -}}
{{ if .HasBasePathMappings -}}

### Base Path Mappings ({{ .BasePathCount }} encontrado(s))
{{ range .BasePathMappings -}}
- **{{ .DomainName }}** / {{ if .BasePath }}{{ .BasePath }}{{ else }}(root){{ end }} → stage: {{ .Stage }}
{{ end -}}
{{ end -}}
{{ if .IsPrivate -}}

### API PRIVATE com Resource Policy
Um arquivo ` + "`policy.json`" + ` foi gerado com a policy atual.
Descomente a linha ` + "`apigateway_policy`" + ` no ` + "`main.tf`" + ` após validar a policy.
{{ end -}}

---

## Itens que requerem revisão manual

{{ if .ReviewIssues -}}
{{ range .ReviewIssues -}}
### {{ .Code }}
{{ .Message }}

{{ end -}}
{{ else -}}
Nenhum item requer revisão manual.

{{ end -}}

---

## Itens bloqueadores

{{ if .BlockIssues -}}
{{ range .BlockIssues -}}
### {{ .Code }}
{{ .Message }}

{{ end -}}
{{ else -}}
Nenhum bloqueador encontrado.

{{ end -}}

---

## Próximos passos

{{ if eq .Tier "AUTO" -}}
1. Revisar o ` + "`main.tf`" + ` e ` + "`openapi.json`" + ` gerados
2. Executar: ` + "`terraform init`" + `
3. Executar os comandos em ` + "`_import_commands.sh`" + `
4. Executar: ` + "`terraform plan`" + ` — verificar zero diff
5. Executar: ` + "`terraform apply`" + ` (se plano aprovado)
{{ else if eq .Tier "BLOCK" -}}
1. Resolver os bloqueadores listados acima
2. Re-executar: ` + "`migration-cli apigw migrate --api-id {{ .APIID }}`" + `
{{ else -}}
1. Revisar os itens marcados acima
2. Ajustar ` + "`main.tf`" + ` e ` + "`openapi.json`" + ` conforme necessário
3. Executar: ` + "`terraform init`" + `
4. Executar os comandos em ` + "`_import_commands.sh`" + `
5. Executar: ` + "`terraform plan`" + ` — verificar cuidadosamente
6. Obter aprovação do time antes de aplicar
{{ end }}
---
*Gerado automaticamente por migration-cli v1.0.0*
`
	var reviewIssues, blockIssues []Issue
	for _, issue := range apicfg.Issues {
		switch issue.Severity {
		case "review":
			reviewIssues = append(reviewIssues, issue)
		case "block":
			blockIssues = append(blockIssues, issue)
		}
	}

	var stageNames []string
	for _, s := range apicfg.Stages {
		stageNames = append(stageNames, s.Name)
	}

	data := struct {
		GeneratedAt      string
		Name             string
		APIID            string
		Product          string
		Team             string
		Env              string
		EndpointType     string
		Pattern          string
		Tier             string
		StageList        string
		ResourceCount    int
		HasLegacyName    bool
		HasAuthorizers   bool
		AuthorizerCount  int
		Authorizers      []AuthorizerInfo
		HasUsagePlans    bool
		UsagePlanCount   int
		UsagePlans       []UsagePlanInfo
		HasBasePathMappings bool
		BasePathCount    int
		BasePathMappings []BasePathMapping
		IsPrivate        bool
		ReviewIssues     []Issue
		BlockIssues      []Issue
	}{
		GeneratedAt:         time.Now().Format("2006-01-02 15:04:05"),
		Name:                apicfg.Name,
		APIID:               apicfg.ID,
		Product:             apicfg.Product,
		Team:                apicfg.Team,
		Env:                 apicfg.Env,
		EndpointType:        apicfg.EndpointType,
		Pattern:             string(apicfg.Pattern),
		Tier:                string(apicfg.Tier),
		StageList:           strings.Join(stageNames, ", "),
		ResourceCount:       apicfg.ResourceCount,
		HasLegacyName:       apicfg.LegacyName,
		HasAuthorizers:      len(apicfg.Authorizers) > 0,
		AuthorizerCount:     len(apicfg.Authorizers),
		Authorizers:         apicfg.Authorizers,
		HasUsagePlans:       len(apicfg.UsagePlans) > 0,
		UsagePlanCount:      len(apicfg.UsagePlans),
		UsagePlans:          apicfg.UsagePlans,
		HasBasePathMappings: len(apicfg.BasePathMappings) > 0,
		BasePathCount:       len(apicfg.BasePathMappings),
		BasePathMappings:    apicfg.BasePathMappings,
		IsPrivate:           apicfg.EndpointType == "PRIVATE" && apicfg.Policy != "",
		ReviewIssues:        reviewIssues,
		BlockIssues:         blockIssues,
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

// generateImportScript gera o script _import_commands.sh com os comandos de import.
func generateImportScript(apicfg *APIConfig, outputDir string) error {
	var sb strings.Builder

	sb.WriteString("#!/bin/bash\n")
	sb.WriteString(fmt.Sprintf("# Import commands para %s (%s)\n", apicfg.Name, apicfg.ID))
	sb.WriteString("# Execute APÓS terraform init e ANTES de terraform plan\n\n")

	// Import da REST API
	sb.WriteString(fmt.Sprintf("terraform import 'module.apigw.aws_api_gateway_rest_api.this' '%s'\n", apicfg.ID))

	// Import de cada stage
	for _, stage := range apicfg.Stages {
		sb.WriteString(fmt.Sprintf(
			"terraform import 'module.apigw.aws_api_gateway_stage.this[%q]' '%s/%s'\n",
			stage.Name, apicfg.ID, stage.Name,
		))
	}

	// Import de cada base path mapping
	for _, bpm := range apicfg.BasePathMappings {
		basePath := bpm.BasePath
		if basePath == "" {
			basePath = "(none)"
		}
		importKey := fmt.Sprintf("%s/%s", bpm.DomainName, basePath)
		sb.WriteString(fmt.Sprintf(
			"terraform import 'module.apigw.aws_api_gateway_base_path_mapping.this[%q]' '%s/%s'\n",
			importKey, bpm.DomainName, basePath,
		))
	}

	return os.WriteFile(filepath.Join(outputDir, "_import_commands.sh"), []byte(sb.String()), 0755)
}

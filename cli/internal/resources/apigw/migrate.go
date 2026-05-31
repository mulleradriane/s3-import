package apigw

import (
	"context"
	"fmt"
	"path/filepath"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/apigateway"
	"github.com/mulleradriane/migration-cli/internal/resources"
	"github.com/mulleradriane/migration-cli/internal/terraform"
)

// APIGWResource implementa a interface resources.Resource para API Gateway.
type APIGWResource struct{}

// New retorna uma nova instância de APIGWResource.
func New() *APIGWResource {
	return &APIGWResource{}
}

// Name retorna o identificador do recurso.
func (r *APIGWResource) Name() string {
	return "apigw"
}

// Discover lista e classifica APIs Gateway via AWS.
// Para manter compatibilidade com a interface Resource, opts.Prefix é usado como filtro de prefixo.
func (r *APIGWResource) Discover(ctx context.Context, cfg aws.Config, opts resources.DiscoverOpts) ([]resources.DiscoveryResult, error) {
	discoverOpts := DiscoverOpts{
		Prefix: opts.Prefix,
		Env:    opts.Env,
	}

	results, err := Discover(ctx, cfg, discoverOpts)
	if err != nil {
		return nil, err
	}

	var out []resources.DiscoveryResult
	for _, r := range results {
		extra := map[string]string{
			"api_id":  r.APIID,
			"pattern": r.Pattern,
			"product": r.Product,
			"env":     r.Env,
			"stages":  fmt.Sprintf("%v", r.Stages),
		}
		out = append(out, resources.DiscoveryResult{
			Name:   r.Name,
			Tier:   r.Tier,
			Reason: r.Reason,
			Extra:  extra,
		})
	}

	return out, nil
}

// Extract extrai a configuração atual da API via AWS APIs.
func (r *APIGWResource) Extract(ctx context.Context, cfg aws.Config, apiID string) (interface{}, error) {
	return Extract(ctx, cfg, apiID)
}

// Generate gera os arquivos Terraform a partir da config extraída.
func (r *APIGWResource) Generate(ctx context.Context, extracted interface{}, opts resources.MigrateOpts) error {
	apicfg, ok := extracted.(*APIConfig)
	if !ok {
		return fmt.Errorf("tipo inválido: esperado *APIConfig, recebido %T", extracted)
	}
	return GenerateFiles(apicfg, opts.OutputDir)
}

// Migrate orquestra o pipeline completo: preflight → extract → generate → tf init → import → plan
func (r *APIGWResource) Migrate(ctx context.Context, awsCfg aws.Config, apiID string, opts resources.MigrateOpts) error {
	outputDir := opts.OutputDir
	if outputDir == "" {
		outputDir = filepath.Join(".", "output", apiID)
	}

	fmt.Printf("\n  Migrando API Gateway: %s\n", apiID)
	fmt.Printf("   Time: %s | Env: %s\n", opts.Team, opts.Env)
	fmt.Printf("   Output: %s\n\n", outputDir)

	// === ETAPA 1: Preflight ===
	fmt.Println("--- [1/5] Preflight Check ---")
	issues, err := Preflight(ctx, awsCfg, apiID)
	if err != nil {
		return fmt.Errorf("preflight falhou: %w", err)
	}

	blockers := filterBlockers(issues)
	if len(blockers) > 0 {
		fmt.Println("Bloqueadores encontrados:")
		for _, b := range blockers {
			fmt.Printf("   * [%s] %s\n", b.Code, b.Message)
		}
		return fmt.Errorf("preflight bloqueou a migração: %d bloqueador(es) encontrado(s)", len(blockers))
	}
	fmt.Println("OK — Preflight sem bloqueadores")

	// === ETAPA 2: Extract ===
	fmt.Println("\n--- [2/5] Extração de Configuração ---")
	apicfg, err := Extract(ctx, awsCfg, apiID)
	if err != nil {
		return fmt.Errorf("extração falhou: %w", err)
	}

	// Enriquece com metadados
	apicfg.Team = opts.Team
	if opts.Env != "" {
		apicfg.Env = opts.Env
	}

	fmt.Printf("OK — Config extraída — Tier: %s | Pattern: %s\n", apicfg.Tier, apicfg.Pattern)

	if apicfg.Tier == TierBLOCK {
		fmt.Println("Tier BLOCK — salvando config e gerando CHANGES.md com bloqueadores")
		if err := SaveAPIConfig(apicfg, outputDir); err != nil {
			fmt.Printf("Aviso: nao foi possivel salvar .apigwconfig.json: %v\n", err)
		}
		if err := generateChangesMD(apicfg, outputDir); err != nil {
			fmt.Printf("Aviso: nao foi possivel gerar CHANGES.md: %v\n", err)
		}
		return fmt.Errorf("API %q está no tier BLOCK — não pode ser migrada automaticamente", apiID)
	}

	// Salva a config extraída
	if err := SaveAPIConfig(apicfg, outputDir); err != nil {
		return fmt.Errorf("falha ao salvar config: %w", err)
	}
	fmt.Printf("   Config salva em: %s/.apigwconfig.json\n", outputDir)

	// === ETAPA 3: Generate ===
	fmt.Println("\n--- [3/5] Geração de Arquivos Terraform ---")

	if opts.DryRun {
		fmt.Println("DRY-RUN: gerando arquivos sem executar Terraform")
	}

	if err := GenerateFiles(apicfg, outputDir); err != nil {
		return fmt.Errorf("geração de arquivos falhou: %w", err)
	}

	fmt.Printf("OK — Arquivos gerados em %s/:\n", outputDir)
	fmt.Println("   * openapi.json")
	fmt.Println("   * main.tf")
	fmt.Println("   * backend.tf")
	fmt.Println("   * CHANGES.md")
	fmt.Println("   * _import_commands.sh")

	if apicfg.Tier != TierAUTO {
		fmt.Println("\nTier REVIEW — os seguintes itens requerem atencao:")
		for _, issue := range apicfg.Issues {
			if issue.Severity == "review" {
				fmt.Printf("   * [%s] %s\n", issue.Code, issue.Message)
			}
		}
	}

	if opts.DryRun {
		fmt.Println("\nDRY-RUN: pipeline concluído (Terraform não executado)")
		return nil
	}

	// === ETAPA 4: Terraform Init ===
	fmt.Println("\n--- [4/5] Terraform Init ---")
	runner := terraform.NewRunner(outputDir)

	if err := runner.Init(ctx); err != nil {
		return fmt.Errorf("terraform init falhou: %w", err)
	}
	fmt.Println("OK — Terraform init concluído")

	// === ETAPA 5a: Terraform Import ===
	if !opts.SkipImport {
		fmt.Println("\n--- [5a/5] Terraform Import ---")

		// Import da REST API
		importAddr := "module.apigw.aws_api_gateway_rest_api.this"
		if err := runner.Import(ctx, importAddr, apiID); err != nil {
			return fmt.Errorf("terraform import (rest_api) falhou: %w", err)
		}
		fmt.Printf("OK — REST API importada: %s -> %s\n", apiID, importAddr)

		// Import de cada stage
		for _, stage := range apicfg.Stages {
			stageAddr := fmt.Sprintf("module.apigw.aws_api_gateway_stage.this[%q]", stage.Name)
			stageID := fmt.Sprintf("%s/%s", apiID, stage.Name)
			if err := runner.Import(ctx, stageAddr, stageID); err != nil {
				fmt.Printf("Aviso: import do stage %q falhou: %v\n", stage.Name, err)
			} else {
				fmt.Printf("OK — Stage importado: %s\n", stage.Name)
			}
		}

		// Import de base path mappings
		for _, bpm := range apicfg.BasePathMappings {
			basePath := bpm.BasePath
			if basePath == "" {
				basePath = "(none)"
			}
			importKey := fmt.Sprintf("%s/%s", bpm.DomainName, basePath)
			bpmAddr := fmt.Sprintf("module.apigw.aws_api_gateway_base_path_mapping.this[%q]", importKey)
			bpmID := fmt.Sprintf("%s/%s", bpm.DomainName, basePath)
			if err := runner.Import(ctx, bpmAddr, bpmID); err != nil {
				fmt.Printf("Aviso: import do base path mapping %q falhou: %v\n", importKey, err)
			} else {
				fmt.Printf("OK — Base path mapping importado: %s\n", importKey)
			}
		}
	} else {
		fmt.Println("\n[5a/5] Terraform Import — pulado (--skip-import)")
	}

	// === ETAPA 5b: Terraform Plan ===
	if !opts.SkipPlan {
		fmt.Println("\n--- [5b/5] Terraform Plan ---")
		if err := runner.Plan(ctx); err != nil {
			return fmt.Errorf("terraform plan falhou: %w", err)
		}
		fmt.Println("OK — Terraform plan concluído — revise o output acima")
	} else {
		fmt.Println("\n[5b/5] Terraform Plan — pulado (--skip-plan)")
	}

	fmt.Printf("\nOK — Migração de %q concluída com sucesso!\n", apiID)
	if apicfg.Tier != TierAUTO {
		fmt.Println("   Revise o CHANGES.md e obtenha aprovação antes de aplicar")
	}

	return nil
}

// Preflight verifica bloqueadores na API sem gerar nada.
func Preflight(ctx context.Context, awsCfg aws.Config, apiID string) ([]Issue, error) {
	client := apigateway.NewFromConfig(awsCfg)

	resp, err := client.GetRestApi(ctx, &apigateway.GetRestApiInput{
		RestApiId: aws.String(apiID),
	})
	if err != nil {
		return nil, fmt.Errorf("falha ao buscar API %q: %w", apiID, err)
	}

	var issues []Issue
	apiName := aws.ToString(resp.Name)

	// Verifica duplicidade de nome (requer lista de todas as APIs)
	apisResp, err := client.GetRestApis(ctx, &apigateway.GetRestApisInput{
		Limit: aws.Int32(500),
	})
	if err == nil {
		count := 0
		for _, a := range apisResp.Items {
			if aws.ToString(a.Name) == apiName {
				count++
			}
		}
		if count > 1 {
			issues = append(issues, Issue{
				Severity: "block",
				Code:     "DUPLICATE_NAME",
				Message:  fmt.Sprintf("nome %q duplicado: %d APIs com este nome", apiName, count),
			})
		}
	}

	// Verifica resource count
	resourcesResp, err := client.GetResources(ctx, &apigateway.GetResourcesInput{
		RestApiId: aws.String(apiID),
		Limit:     aws.Int32(500),
	})
	if err == nil {
		count := len(resourcesResp.Items)
		if count >= limitThreshold {
			issues = append(issues, Issue{
				Severity: "review",
				Code:     "NEAR_RESOURCE_LIMIT",
				Message:  fmt.Sprintf("resource count=%d (limite=500) — migração pode falhar se houver crescimento", count),
			})
		}
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

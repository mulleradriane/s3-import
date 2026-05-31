package apigw

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/apigateway"
	apigwtypes "github.com/aws/aws-sdk-go-v2/service/apigateway/types"
)

// envPatterns são os padrões que indicam URI com env hardcoded (Tier 3).
var envPatterns = []string{
	"-dev.", "-hml.", "-prd.",
	".dev.", ".hml.", ".prd.",
	"/dev/", "/hml/", "/prd/",
	"dev-", "hml-", "prd-",
}

// Extract extrai a configuração completa de uma API Gateway via AWS APIs.
func Extract(ctx context.Context, cfg aws.Config, apiID string) (*APIConfig, error) {
	client := apigateway.NewFromConfig(cfg)

	apicfg := &APIConfig{}

	// 1. GetRestApi — info base + policy
	if err := extractRestAPI(ctx, client, apiID, apicfg); err != nil {
		return nil, fmt.Errorf("falha ao extrair rest api: %w", err)
	}

	// 2. GetResources com embed=methods — paths + integrations
	if err := extractIntegrations(ctx, client, apiID, apicfg); err != nil {
		// Não fatal — anota o issue e continua
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_RESOURCES_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair resources/integrations: %v", err),
		})
	}

	// 3. GetStages — stages com certs e throttling
	stages, err := getStages(ctx, client, apiID)
	if err != nil {
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_STAGES_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair stages: %v", err),
		})
	} else {
		apicfg.Stages = stages
	}

	// 4. GetAuthorizers
	if err := extractAuthorizers(ctx, client, apiID, apicfg); err != nil {
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_AUTHORIZERS_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair authorizers: %v", err),
		})
	}

	// 5. GetUsagePlans — filtrar os do apiID via ApiStages
	if err := extractUsagePlans(ctx, client, apiID, apicfg); err != nil {
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_USAGEPLANS_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair usage plans: %v", err),
		})
	}

	// 6. GetBasePathMappings via GetDomainNames
	if err := extractBasePathMappings(ctx, client, apiID, apicfg); err != nil {
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_BASEPATH_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair base path mappings: %v", err),
		})
	}

	// 7. GetExport OAS3 — usa o primeiro stage disponível
	if err := extractOAS3Export(ctx, client, apiID, apicfg); err != nil {
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "review",
			Code:     "EXTRACT_OAS3_ERROR",
			Message:  fmt.Sprintf("Não foi possível exportar OAS3: %v", err),
		})
	}

	// Classifica o tier com base nas integrações extraídas
	classifyTier(apicfg)

	return apicfg, nil
}

// extractRestAPI busca as informações base da API.
func extractRestAPI(ctx context.Context, client *apigateway.Client, apiID string, apicfg *APIConfig) error {
	resp, err := client.GetRestApi(ctx, &apigateway.GetRestApiInput{
		RestApiId: aws.String(apiID),
	})
	if err != nil {
		return err
	}

	apicfg.ID = aws.ToString(resp.Id)
	apicfg.Name = aws.ToString(resp.Name)
	apicfg.Description = aws.ToString(resp.Description)
	apicfg.Policy = aws.ToString(resp.Policy)
	apicfg.Tags = resp.Tags

	if resp.CreatedDate != nil {
		apicfg.CreatedDate = *resp.CreatedDate
	}

	if resp.EndpointConfiguration != nil && len(resp.EndpointConfiguration.Types) > 0 {
		apicfg.EndpointType = string(resp.EndpointConfiguration.Types[0])
	} else {
		apicfg.EndpointType = "EDGE"
	}

	// Extrai metadados de produto/env do nome
	product, env, pattern, legacy := parseAPIName(apicfg.Name)
	apicfg.Product = product
	apicfg.Env = env
	apicfg.Pattern = pattern
	apicfg.LegacyName = legacy

	return nil
}

// extractIntegrations analisa as integrações para detectar Tier 2 (VPC Link) e Tier 3 (URI hardcoded).
func extractIntegrations(ctx context.Context, client *apigateway.Client, apiID string, apicfg *APIConfig) error {
	var position *string

	for {
		resp, err := client.GetResources(ctx, &apigateway.GetResourcesInput{
			RestApiId: aws.String(apiID),
			Embed:     []string{"methods"},
			Limit:     aws.Int32(500),
			Position:  position,
		})
		if err != nil {
			return err
		}

		apicfg.ResourceCount += len(resp.Items)

		for _, resource := range resp.Items {
			path := aws.ToString(resource.Path)

			for httpMethod, method := range resource.ResourceMethods {
				if method.MethodIntegration == nil {
					continue
				}

				integration := method.MethodIntegration
				intType := string(integration.Type)
				uri := aws.ToString(integration.Uri)

				is := IntegrationSummary{
					ResourcePath: path,
					Method:       httpMethod,
					Type:         intType,
					URI:          uri,
				}

				// Detecção de VPC Link (Tier 2)
				if integration.ConnectionType == apigwtypes.ConnectionTypeVpcLink {
					is.Type = "VPC_LINK"
				}

				// Detecção de URI hardcoded (Tier 3)
				if uri != "" {
					is.HasHardcodedEnv = hasHardcodedEnv(uri)
				}

				apicfg.Integrations = append(apicfg.Integrations, is)
			}
		}

		if resp.Position == nil {
			break
		}
		position = resp.Position
	}

	return nil
}

// hasHardcodedEnv verifica se a URI contém um indicador de ambiente hardcoded.
// Ignora stage variables como {stageVariables.env}.
func hasHardcodedEnv(uri string) bool {
	// Se a URI usa stage variables, não é hardcoded
	if strings.Contains(uri, "{stageVariables.") {
		return false
	}

	// Tenta parsear como URL para verificar hostname/path
	u, err := url.Parse(uri)
	if err != nil {
		// Não é URL válida — verifica a string inteira
		uriLower := strings.ToLower(uri)
		for _, pattern := range envPatterns {
			if strings.Contains(uriLower, pattern) {
				return true
			}
		}
		return false
	}

	hostAndPath := strings.ToLower(u.Host + u.Path)
	for _, pattern := range envPatterns {
		if strings.Contains(hostAndPath, pattern) {
			return true
		}
	}
	return false
}

// extractAuthorizers busca os authorizers da API.
func extractAuthorizers(ctx context.Context, client *apigateway.Client, apiID string, apicfg *APIConfig) error {
	resp, err := client.GetAuthorizers(ctx, &apigateway.GetAuthorizersInput{
		RestApiId: aws.String(apiID),
	})
	if err != nil {
		return err
	}

	for _, a := range resp.Items {
		ai := AuthorizerInfo{
			ID:           aws.ToString(a.Id),
			Name:         aws.ToString(a.Name),
			Type:         string(a.Type),
			ProviderARNs: a.ProviderARNs,
			URI:          aws.ToString(a.AuthorizerUri),
		}
		apicfg.Authorizers = append(apicfg.Authorizers, ai)
	}

	return nil
}

// extractUsagePlans busca os usage plans associados à API.
func extractUsagePlans(ctx context.Context, client *apigateway.Client, apiID string, apicfg *APIConfig) error {
	var position *string

	for {
		resp, err := client.GetUsagePlans(ctx, &apigateway.GetUsagePlansInput{
			Position: position,
			Limit:    aws.Int32(500),
		})
		if err != nil {
			return err
		}

		for _, plan := range resp.Items {
			// Verifica se este usage plan está associado ao apiID
			associated := false
			for _, stage := range plan.ApiStages {
				if aws.ToString(stage.ApiId) == apiID {
					associated = true
					break
				}
			}

			if !associated {
				continue
			}

			upi := UsagePlanInfo{
				ID:   aws.ToString(plan.Id),
				Name: aws.ToString(plan.Name),
			}

			if plan.Quota != nil {
				upi.Quota = &UsagePlanQuota{
					Limit:  plan.Quota.Limit,
					Period: string(plan.Quota.Period),
				}
			}

			apicfg.UsagePlans = append(apicfg.UsagePlans, upi)
		}

		if resp.Position == nil {
			break
		}
		position = resp.Position
	}

	return nil
}

// extractBasePathMappings busca os base path mappings de todos os domínios customizados para esta API.
func extractBasePathMappings(ctx context.Context, client *apigateway.Client, apiID string, apicfg *APIConfig) error {
	// Lista todos os domínios customizados
	var domains []string
	var position *string

	for {
		resp, err := client.GetDomainNames(ctx, &apigateway.GetDomainNamesInput{
			Position: position,
			Limit:    aws.Int32(500),
		})
		if err != nil {
			return fmt.Errorf("falha ao listar domain names: %w", err)
		}

		for _, dn := range resp.Items {
			if dn.DomainName != nil {
				domains = append(domains, aws.ToString(dn.DomainName))
			}
		}

		if resp.Position == nil {
			break
		}
		position = resp.Position
	}

	// Para cada domínio, busca os base path mappings e filtra pelo apiID
	for _, domain := range domains {
		var bpmPosition *string

		for {
			resp, err := client.GetBasePathMappings(ctx, &apigateway.GetBasePathMappingsInput{
				DomainName: aws.String(domain),
				Position:   bpmPosition,
				Limit:      aws.Int32(500),
			})
			if err != nil {
				// Alguns domínios podem não ter mapeamentos — não é fatal
				break
			}

			for _, bpm := range resp.Items {
				if aws.ToString(bpm.RestApiId) == apiID {
					apicfg.BasePathMappings = append(apicfg.BasePathMappings, BasePathMapping{
						DomainName: domain,
						BasePath:   aws.ToString(bpm.BasePath),
						Stage:      aws.ToString(bpm.Stage),
					})
				}
			}

			if resp.Position == nil {
				break
			}
			bpmPosition = resp.Position
		}
	}

	return nil
}

// extractOAS3Export exporta a definição OAS3 da API.
func extractOAS3Export(ctx context.Context, client *apigateway.Client, apiID string, apicfg *APIConfig) error {
	// Usa o primeiro stage disponível para o export
	if len(apicfg.Stages) == 0 {
		return fmt.Errorf("nenhum stage disponível para export")
	}

	stageName := apicfg.Stages[0].Name

	resp, err := client.GetExport(ctx, &apigateway.GetExportInput{
		RestApiId:  aws.String(apiID),
		StageName:  aws.String(stageName),
		ExportType: aws.String("oas30"),
		Accepts:    aws.String("application/json"),
		Parameters: map[string]string{
			"extensions": "apigateway",
		},
	})
	if err != nil {
		return err
	}

	apicfg.OAS3Export = resp.Body
	return nil
}

// classifyTier classifica o tier da API com base nas integrações extraídas.
// Atualiza apicfg.Tier e apicfg.Issues em-place.
func classifyTier(apicfg *APIConfig) {
	// Não sobrescreve BLOCK (nome duplicado detectado no discover)
	if apicfg.Tier == TierBLOCK {
		return
	}

	// Não sobrescreve REVIEW_LIMIT
	if apicfg.Tier == TierREVIEW_LIMIT {
		return
	}

	hasVPCLink := false
	hasHardcoded := false

	for _, integ := range apicfg.Integrations {
		if integ.Type == "VPC_LINK" {
			hasVPCLink = true
		}
		if integ.HasHardcodedEnv {
			hasHardcoded = true
		}
	}

	if hasVPCLink {
		apicfg.Tier = TierREVIEW_VPCLINK
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "review",
			Code:     "VPC_LINK_INTEGRATION",
			Message:  "API usa VPC Link — verifique naming e configuração antes de importar",
		})
	} else if hasHardcoded {
		apicfg.Tier = TierREVIEW_HARDCODED
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "review",
			Code:     "HARDCODED_ENV_URI",
			Message:  "URI de integração contém ambiente hardcoded (dev/hml/prd) — use stage variables",
		})
	} else {
		apicfg.Tier = TierAUTO
	}

	// PRIVATE APIs com policy precisam de atenção extra
	if apicfg.EndpointType == "PRIVATE" && apicfg.Policy != "" {
		apicfg.Issues = append(apicfg.Issues, Issue{
			Severity: "review",
			Code:     "PRIVATE_API_POLICY",
			Message:  "API PRIVATE com resource policy — verifique policy.json gerado antes de aplicar",
		})
	}
}

// SaveAPIConfig salva a configuração da API em .apigwconfig.json e openapi.json.
func SaveAPIConfig(apicfg *APIConfig, outputDir string) error {
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		return fmt.Errorf("falha ao criar diretório %q: %w", outputDir, err)
	}

	// Salva .apigwconfig.json (sem OAS3Export inline para não duplicar)
	configCopy := *apicfg
	configCopy.OAS3Export = nil // salvo separadamente

	data, err := json.MarshalIndent(configCopy, "", "  ")
	if err != nil {
		return fmt.Errorf("falha ao serializar config: %w", err)
	}

	if err := os.WriteFile(filepath.Join(outputDir, ".apigwconfig.json"), data, 0644); err != nil {
		return fmt.Errorf("falha ao escrever .apigwconfig.json: %w", err)
	}

	// Salva openapi.json se disponível
	if len(apicfg.OAS3Export) > 0 {
		if err := os.WriteFile(filepath.Join(outputDir, "openapi.json"), apicfg.OAS3Export, 0644); err != nil {
			return fmt.Errorf("falha ao escrever openapi.json: %w", err)
		}
	}

	return nil
}

// LoadAPIConfig carrega a configuração da API de .apigwconfig.json.
func LoadAPIConfig(configPath string) (*APIConfig, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, fmt.Errorf("falha ao ler %q: %w", configPath, err)
	}

	var apicfg APIConfig
	if err := json.Unmarshal(data, &apicfg); err != nil {
		return nil, fmt.Errorf("falha ao deserializar config: %w", err)
	}

	// Tenta carregar openapi.json do mesmo diretório
	dir := filepath.Dir(configPath)
	openapiPath := filepath.Join(dir, "openapi.json")
	if data, err := os.ReadFile(openapiPath); err == nil {
		apicfg.OAS3Export = data
	}

	return &apicfg, nil
}

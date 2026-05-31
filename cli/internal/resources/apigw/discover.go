package apigw

import (
	"context"
	"fmt"
	"strings"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/apigateway"
	apigwtypes "github.com/aws/aws-sdk-go-v2/service/apigateway/types"
)

const (
	maxParallel    = 10
	limitThreshold = 480
)

// Discover lista e classifica todas as REST APIs do API Gateway.
// Executa chamadas em paralelo (até maxParallel goroutines simultâneas).
func Discover(ctx context.Context, cfg aws.Config, opts DiscoverOpts) ([]DiscoveryResult, error) {
	client := apigateway.NewFromConfig(cfg)

	// Lista todas as APIs com paginação
	var allAPIs []apiInfo
	var position *string

	for {
		resp, err := client.GetRestApis(ctx, &apigateway.GetRestApisInput{
			Position: position,
			Limit:    aws.Int32(500),
		})
		if err != nil {
			return nil, fmt.Errorf("falha ao listar APIs: %w", err)
		}

		for _, item := range resp.Items {
			allAPIs = append(allAPIs, apiInfo{
				id:          aws.ToString(item.Id),
				name:        aws.ToString(item.Name),
				description: aws.ToString(item.Description),
				endpointCfg: item.EndpointConfiguration,
				tags:        item.Tags,
			})
		}

		if resp.Position == nil {
			break
		}
		position = resp.Position
	}

	// Detecta nomes duplicados (BLOCK)
	nameCount := make(map[string]int)
	for _, api := range allAPIs {
		nameCount[api.name]++
	}

	// Semáforo para limitar paralelismo
	sem := make(chan struct{}, maxParallel)

	type enriched struct {
		result DiscoveryResult
		err    error
		name   string
	}

	results := make([]enriched, len(allAPIs))
	var wg sync.WaitGroup

	for i, api := range allAPIs {
		wg.Add(1)
		sem <- struct{}{}

		go func(idx int, a apiInfo) {
			defer wg.Done()
			defer func() { <-sem }()

			result, err := discoverSingle(ctx, client, a, nameCount)
			results[idx] = enriched{result: result, err: err, name: a.name}
		}(i, api)
	}

	wg.Wait()

	// Aplica filtros e coleta resultados
	var out []DiscoveryResult
	for _, r := range results {
		if r.err != nil {
			// Erros de API individual não abortam o discover — avisamos e continuamos
			fmt.Printf("  aviso: API %q — erro no discover: %v\n", r.name, r.err)
			continue
		}

		dr := r.result

		// Filtro por prefixo
		if opts.Prefix != "" && !strings.HasPrefix(dr.Name, opts.Prefix) {
			continue
		}

		// Filtro por env
		if opts.Env != "" {
			if dr.Pattern == string(PatternB) {
				if !strings.EqualFold(dr.Env, opts.Env) {
					continue
				}
			}
			// Pattern A: não filtra por env (tem múltiplos stages)
		}

		// Filtro por tier
		if opts.Tier != "" && !strings.EqualFold(dr.Tier, opts.Tier) {
			continue
		}

		out = append(out, dr)
	}

	return out, nil
}

// apiInfo é uma estrutura interna para o discover.
type apiInfo struct {
	id          string
	name        string
	description string
	endpointCfg *apigwtypes.EndpointConfiguration
	tags        map[string]string
}

// discoverSingle enriquece uma API com informações de stages e resource count.
func discoverSingle(ctx context.Context, client *apigateway.Client, api apiInfo, nameCount map[string]int) (DiscoveryResult, error) {
	dr := DiscoveryResult{
		Name:  api.name,
		APIID: api.id,
	}

	// Extrai product/env/pattern do nome
	product, env, pattern, _ := parseAPIName(api.name)
	dr.Product = product
	dr.Env = env
	dr.Pattern = string(pattern)

	// Busca resource count (para detecção de Tier 4)
	resourceCount, err := getResourceCount(ctx, client, api.id)
	if err != nil {
		resourceCount = 0
	}

	// Busca stages
	stages, err := getStages(ctx, client, api.id)
	if err != nil {
		return dr, fmt.Errorf("falha ao buscar stages: %w", err)
	}

	var stageNames []string
	for _, s := range stages {
		stageNames = append(stageNames, s.Name)
	}
	dr.Stages = stageNames

	// Classificação de tier no discover
	tier, reason := classifyDiscoverTier(api.name, nameCount, resourceCount)
	dr.Tier = string(tier)
	dr.Reason = reason

	return dr, nil
}

// getResourceCount retorna o número de resources de uma API.
func getResourceCount(ctx context.Context, client *apigateway.Client, apiID string) (int, error) {
	resp, err := client.GetResources(ctx, &apigateway.GetResourcesInput{
		RestApiId: aws.String(apiID),
		Limit:     aws.Int32(500),
	})
	if err != nil {
		return 0, err
	}
	return len(resp.Items), nil
}

// getStages retorna os stages de uma API.
func getStages(ctx context.Context, client *apigateway.Client, apiID string) ([]StageInfo, error) {
	resp, err := client.GetStages(ctx, &apigateway.GetStagesInput{
		RestApiId: aws.String(apiID),
	})
	if err != nil {
		return nil, err
	}

	var stages []StageInfo
	for _, s := range resp.Item {
		si := StageInfo{
			Name:         aws.ToString(s.StageName),
			Description:  aws.ToString(s.Description),
			DeploymentID: aws.ToString(s.DeploymentId),
		}
		if s.ClientCertificateId != nil {
			si.ClientCertificateID = aws.ToString(s.ClientCertificateId)
		}
		if s.AccessLogSettings != nil {
			si.AccessLogARN = aws.ToString(s.AccessLogSettings.DestinationArn)
		}
		// Throttling a nível de stage vem de MethodSettings (/* path)
		if ms, ok := s.MethodSettings["*/*"]; ok {
			si.ThrottlingBurst = ms.ThrottlingBurstLimit
			si.ThrottlingRate = ms.ThrottlingRateLimit
		}
		stages = append(stages, si)
	}

	return stages, nil
}

// classifyDiscoverTier classifica o tier no discover (sem analisar integrações).
func classifyDiscoverTier(name string, nameCount map[string]int, resourceCount int) (APITier, string) {
	// BLOCK: nome duplicado
	if nameCount[name] > 1 {
		return TierBLOCK, fmt.Sprintf("nome duplicado: %d APIs com o nome %q", nameCount[name], name)
	}

	// REVIEW_LIMIT: próximo do limite de 500 resources
	if resourceCount >= limitThreshold {
		return TierREVIEW_LIMIT, fmt.Sprintf("resourceCount=%d (>= %d — próximo do limite de 500)", resourceCount, limitThreshold)
	}

	// AUTO provisório — Tier 2/3 detectados apenas no extract
	return TierAUTO, "classificação provisória (Tier 2/3 detectado somente no extract)"
}

// parseAPIName extrai product, env e pattern do nome da API.
// Exemplos:
//
//	"apigw-score-prd"  → product="score",      env="prd",  pattern=B, legacy=false
//	"apigw-collection" → product="collection",  env="",     pattern=A, legacy=false
//	"heimdall"         → product="heimdall",     env="",     pattern=A, legacy=true
func parseAPIName(name string) (product, env string, pattern NamingPattern, legacy bool) {
	// Remove o prefixo "apigw-" se existir
	if !strings.HasPrefix(name, "apigw-") {
		// Nome legacy: não começa com "apigw-"
		legacy = true
		product = name
		pattern = PatternA
		return
	}

	withoutPrefix := strings.TrimPrefix(name, "apigw-")

	// Verifica se termina com sufixo de env (Pattern B)
	for suffix, envName := range knownEnvNames {
		if strings.HasSuffix(withoutPrefix, "-"+suffix) {
			product = strings.TrimSuffix(withoutPrefix, "-"+suffix)
			env = envName
			pattern = PatternB
			return
		}
	}

	// Sem sufixo de env → Pattern A
	product = withoutPrefix
	pattern = PatternA
	return
}

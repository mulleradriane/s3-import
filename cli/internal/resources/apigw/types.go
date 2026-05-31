package apigw

import "time"

// APITier representa o nível de automação possível para uma API Gateway.
type APITier string

const (
	TierAUTO             APITier = "AUTO"             // Tier 1: import direto
	TierREVIEW_VPCLINK   APITier = "REVIEW_VPCLINK"   // Tier 2: VPC Link com naming inconsistente
	TierREVIEW_HARDCODED APITier = "REVIEW_HARDCODED" // Tier 3: URI com env hardcoded
	TierREVIEW_LIMIT     APITier = "REVIEW_LIMIT"     // Tier 4: perto do limite de 500 resources
	TierBLOCK            APITier = "BLOCK"
)

// NamingPattern representa o padrão de naming da API.
type NamingPattern string

const (
	PatternA NamingPattern = "A" // 1 API para todos os envs (apigw-{product})
	PatternB NamingPattern = "B" // 1 API por env (apigw-{product}-{env})
)

// Issue descreve um problema encontrado durante a análise da API.
type Issue struct {
	Severity string // "block", "review", "info"
	Code     string // Código curto do problema
	Message  string // Mensagem para o humano
}

// StageInfo contém informações sobre um stage da API.
type StageInfo struct {
	Name                string
	Description         string
	ClientCertificateID string
	AccessLogARN        string
	ThrottlingBurst     int32
	ThrottlingRate      float64
	DeploymentID        string
}

// AuthorizerInfo contém informações sobre um authorizer da API.
type AuthorizerInfo struct {
	ID           string
	Name         string
	Type         string   // TOKEN, REQUEST, COGNITO_USER_POOLS
	ProviderARNs []string
	URI          string   // Lambda ARN para REQUEST/TOKEN
}

// UsagePlanQuota define o limite de chamadas de um usage plan.
type UsagePlanQuota struct {
	Limit  int32
	Period string // DAY, WEEK, MONTH
}

// UsagePlanInfo contém informações sobre um usage plan associado à API.
type UsagePlanInfo struct {
	ID    string
	Name  string
	Quota *UsagePlanQuota
}

// BasePathMapping representa o mapeamento de base path de um domínio customizado para a API.
type BasePathMapping struct {
	DomainName string
	BasePath   string
	Stage      string
}

// IntegrationSummary representa um resumo de integração de um resource/método.
type IntegrationSummary struct {
	ResourcePath    string
	Method          string
	Type            string // MOCK, AWS_PROXY, HTTP_PROXY, VPC_LINK
	URI             string
	HasHardcodedEnv bool // URI contém "dev", "hml", "prd" literal
}

// APIInfo contém as informações básicas de uma API Gateway.
type APIInfo struct {
	ID           string
	Name         string
	Description  string
	EndpointType string // EDGE, REGIONAL, PRIVATE
	Policy       string // JSON da resource policy
	CreatedDate  time.Time
	Tags         map[string]string

	// Derivados no discover
	Pattern       NamingPattern
	Stages        []StageInfo
	ResourceCount int
	Tier          APITier
	Issues        []Issue

	// Metadados de entrada
	Team    string
	Env     string    // para Pattern B; vazio para Pattern A
	Product string    // extraído do nome (strip "apigw-" e "-{env}")
	LegacyName bool   // true quando nome não começa com "apigw-"
}

// APIConfig é a configuração completa de uma API Gateway, extraída via AWS APIs.
type APIConfig struct {
	APIInfo
	OAS3Export       []byte           // JSON do export OAS3
	Authorizers      []AuthorizerInfo
	UsagePlans       []UsagePlanInfo
	BasePathMappings []BasePathMapping
	Integrations     []IntegrationSummary // populado no extract detalhado
}

// DiscoverOpts contém os filtros opcionais para o discover.
type DiscoverOpts struct {
	Prefix string // filtra por prefixo do nome
	Env    string // filtra por env detectado
	Tier   string // filtra por tier (AUTO, REVIEW_VPCLINK, BLOCK, etc.)
}

// DiscoveryResult é o resultado de discovery de uma única API.
type DiscoveryResult struct {
	Name    string
	APIID   string
	Tier    string
	Reason  string
	Pattern string
	Product string
	Env     string
	Team    string
	Stages  []string
}

// envSuffixes é a lista de sufixos de ambiente reconhecidos para Pattern B.
var envSuffixes = []string{"-dev", "-hml", "-prd", "-prod", "-staging", "-sandbox"}

// knownEnvNames é o mapa de sufixo → nome canônico.
var knownEnvNames = map[string]string{
	"dev":     "dev",
	"hml":     "hml",
	"prd":     "prd",
	"prod":    "prd",
	"staging": "staging",
	"sandbox": "sandbox",
}

// BackendBucket retorna o nome do bucket de estado Terraform.
func (a *APIInfo) BackendBucket() string {
	return "ecs-" + a.Team + "-default-aws-terraform"
}

// BackendKey retorna a chave do estado Terraform.
func (a *APIInfo) BackendKey() string {
	if a.Pattern == PatternB && a.Env != "" {
		return "services/apigw/" + a.Product + "/" + a.Env + "/terraform.tfstate"
	}
	return "services/apigw/" + a.Product + "/terraform.tfstate"
}

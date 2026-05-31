package s3

import "time"

// Tier representa o nível de automação possível para um bucket.
type Tier string

const (
	TierAUTO   Tier = "AUTO"   // Aplica direto, sem revisão manual
	TierREVIEW Tier = "REVIEW" // Gera TF mas avisa no CHANGES.md
	TierBLOCK  Tier = "BLOCK"  // Sai do pipeline com erro
)

// ValidAssetCategories é a lista de categorias válidas para asset_category.
var ValidAssetCategories = map[string]bool{
	"Productive data": true,
	"Code":            true,
	"Logs":            true,
	"Cache":           true,
	"Backup":          true,
	"Temporary data":  true,
	"Configuration":   true,
}

// Issue descreve um problema encontrado durante a análise do bucket.
type Issue struct {
	Severity string // "block", "review", "info"
	Code     string // Código curto do problema
	Message  string // Mensagem para o humano
}

// EncryptionConfig representa a configuração de criptografia do bucket.
type EncryptionConfig struct {
	Enabled   bool
	Algorithm string // AES256, aws:kms
	KMSKeyID  string
}

// LifecycleRule representa uma regra de lifecycle do bucket.
type LifecycleRule struct {
	ID     string
	Status string // Enabled, Disabled
	Filter LifecycleFilter
	// Transitions
	Transitions       []LifecycleTransition
	Expiration        *LifecycleExpiration
	NoncurrentVersion *NoncurrentVersionExpiration
}

// LifecycleFilter filtra objetos para uma regra de lifecycle.
type LifecycleFilter struct {
	Prefix string
	Tags   map[string]string
}

// LifecycleTransition define uma transição de storage class.
type LifecycleTransition struct {
	Days         int32
	StorageClass string
}

// LifecycleExpiration define a expiração de objetos.
type LifecycleExpiration struct {
	Days int32
	Date *time.Time
}

// NoncurrentVersionExpiration define a expiração de versões não-correntes.
type NoncurrentVersionExpiration struct {
	Days int32
}

// VersioningConfig representa a configuração de versionamento.
type VersioningConfig struct {
	Status    string // Enabled, Suspended, ""
	MFADelete string
}

// CORSRule representa uma regra de CORS.
type CORSRule struct {
	AllowedHeaders []string
	AllowedMethods []string
	AllowedOrigins []string
	ExposeHeaders  []string
	MaxAgeSeconds  int32
}

// LoggingConfig representa a configuração de logging.
type LoggingConfig struct {
	TargetBucket string
	TargetPrefix string
}

// ReplicationConfig representa a configuração de replicação.
type ReplicationConfig struct {
	Role  string
	Rules []ReplicationRule
}

// ReplicationRule representa uma regra de replicação.
type ReplicationRule struct {
	ID          string
	Status      string
	Destination string
	Prefix      string
}

// ACLConfig representa a configuração de ACL do bucket.
type ACLConfig struct {
	// CannedACL é a ACL padrão (private, public-read, etc.)
	CannedACL string
}

// PublicAccessBlock representa a configuração de bloqueio de acesso público.
type PublicAccessBlock struct {
	BlockPublicAcls       bool
	IgnorePublicAcls      bool
	BlockPublicPolicy     bool
	RestrictPublicBuckets bool
}

// ObjectLockConfig representa a configuração de Object Lock.
type ObjectLockConfig struct {
	Enabled         bool
	Mode            string // GOVERNANCE, COMPLIANCE
	RetentionDays   int32
	RetentionYears  int32
}

// WebsiteConfig representa a configuração de website estático.
type WebsiteConfig struct {
	IndexDocument string
	ErrorDocument string
	RedirectAll   string
}

// Tag representa uma tag AWS.
type Tag struct {
	Key   string
	Value string
}

// BucketConfig é a configuração completa de um bucket S3, extraída via AWS APIs.
type BucketConfig struct {
	BucketName string
	Region     string

	Encryption        EncryptionConfig
	Lifecycle         []LifecycleRule
	Versioning        VersioningConfig
	CORS              []CORSRule
	Logging           LoggingConfig
	Policy            string // JSON da bucket policy
	Website           *WebsiteConfig
	Replication       *ReplicationConfig
	ACL               ACLConfig
	PublicAccessBlock PublicAccessBlock
	ObjectLock        ObjectLockConfig
	Tags              []Tag

	// Campos derivados
	Tier   Tier
	Issues []Issue

	// Metadados de entrada (do CSV)
	Team          string
	Env           string
	AssetCategory string
	Blockers      string
}

// CSVRecord representa uma linha do CSV de levantamento.
type CSVRecord struct {
	BucketName    string
	Team          string
	Env           string
	AssetCategory string
	Category      string
	Blockers      string
}

// LogicalName retorna o nome lógico do bucket (sem o prefixo team-env-).
// Ex: "myteam-dev-data-lake" com team="myteam" env="dev" → "data-lake"
func (b *BucketConfig) LogicalName() string {
	prefix := b.Team + "-" + b.Env + "-"
	if len(b.BucketName) > len(prefix) && b.BucketName[:len(prefix)] == prefix {
		return b.BucketName[len(prefix):]
	}
	return b.BucketName
}

// BackendBucket retorna o nome do bucket de estado Terraform.
func (b *BucketConfig) BackendBucket() string {
	return "ecs-" + b.Team + "-default-aws-terraform"
}

// BackendKey retorna a chave do estado Terraform.
func (b *BucketConfig) BackendKey() string {
	return "services/s3/" + b.LogicalName() + "/" + b.Env + "/terraform.tfstate"
}

// HasReviewIssues retorna true se o bucket tem issues de nível REVIEW.
func (b *BucketConfig) HasReviewIssues() bool {
	for _, issue := range b.Issues {
		if issue.Severity == "review" {
			return true
		}
	}
	return false
}

// ExpectedKMSAlias retorna o alias KMS esperado pelo padrão BP.
func ExpectedKMSAlias(product, env string) string {
	return "alias/" + product + "-default-" + env
}

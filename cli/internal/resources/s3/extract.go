package s3

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

// ExtractBucketConfig extrai a configuração completa de um bucket S3 via AWS APIs.
// Equivalente ao s3_config_extractor.py Python.
func ExtractBucketConfig(ctx context.Context, awsCfg aws.Config, bucketName string) (*BucketConfig, error) {
	client := s3.NewFromConfig(awsCfg)

	cfg := &BucketConfig{
		BucketName: bucketName,
		Region:     awsCfg.Region,
	}

	// Executa todas as chamadas de API em paralelo para performance
	type apiResult struct {
		name string
		err  error
	}

	// Encryption
	if err := extractEncryption(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_ENCRYPTION_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair encryption: %v", err),
		})
	}

	// Lifecycle
	if err := extractLifecycle(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_LIFECYCLE_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair lifecycle: %v", err),
		})
	}

	// Versioning
	if err := extractVersioning(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_VERSIONING_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair versioning: %v", err),
		})
	}

	// CORS
	if err := extractCORS(ctx, client, cfg); err != nil && !isNoSuchCORSConfig(err) {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_CORS_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair CORS: %v", err),
		})
	}

	// Logging
	if err := extractLogging(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_LOGGING_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair logging: %v", err),
		})
	}

	// Policy
	if err := extractPolicy(ctx, client, cfg); err != nil && !isNoSuchBucketPolicy(err) {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_POLICY_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair bucket policy: %v", err),
		})
	}

	// Website
	if err := extractWebsite(ctx, client, cfg); err != nil && !isNoSuchWebsiteConfig(err) {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_WEBSITE_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair website config: %v", err),
		})
	}

	// Replication
	if err := extractReplication(ctx, client, cfg); err != nil && !isReplicationNotFound(err) {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_REPLICATION_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair replication: %v", err),
		})
	}

	// ACL
	if err := extractACL(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_ACL_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair ACL: %v", err),
		})
	}

	// Public Access Block
	if err := extractPublicAccessBlock(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_PAB_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair Public Access Block: %v", err),
		})
	}

	// Object Lock
	if err := extractObjectLock(ctx, client, cfg); err != nil && !isObjectLockNotEnabled(err) {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_OBJECTLOCK_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair Object Lock: %v", err),
		})
	}

	// Tags
	if err := extractTags(ctx, client, cfg); err != nil {
		cfg.Issues = append(cfg.Issues, Issue{
			Severity: "info",
			Code:     "EXTRACT_TAGS_ERROR",
			Message:  fmt.Sprintf("Não foi possível extrair tags: %v", err),
		})
	}

	return cfg, nil
}

func extractEncryption(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketEncryption(ctx, &s3.GetBucketEncryptionInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	if resp.ServerSideEncryptionConfiguration != nil &&
		len(resp.ServerSideEncryptionConfiguration.Rules) > 0 {
		rule := resp.ServerSideEncryptionConfiguration.Rules[0]
		if rule.ApplyServerSideEncryptionByDefault != nil {
			cfg.Encryption.Enabled = true
			cfg.Encryption.Algorithm = string(rule.ApplyServerSideEncryptionByDefault.SSEAlgorithm)
			if rule.ApplyServerSideEncryptionByDefault.KMSMasterKeyID != nil {
				cfg.Encryption.KMSKeyID = aws.ToString(rule.ApplyServerSideEncryptionByDefault.KMSMasterKeyID)
			}
		}
	}

	return nil
}

func extractLifecycle(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketLifecycleConfiguration(ctx, &s3.GetBucketLifecycleConfigurationInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		if isNoSuchLifecycleConfig(err) {
			return nil
		}
		return err
	}

	for _, rule := range resp.Rules {
		lr := LifecycleRule{
			ID:     aws.ToString(rule.ID),
			Status: string(rule.Status),
		}

		// Filter
		if rule.Filter != nil {
			switch f := rule.Filter.(type) {
			case *types.LifecycleRuleFilterMemberPrefix:
				lr.Filter.Prefix = f.Value
			case *types.LifecycleRuleFilterMemberTag:
				lr.Filter.Tags = map[string]string{
					aws.ToString(f.Value.Key): aws.ToString(f.Value.Value),
				}
			}
		}
		if rule.Prefix != nil {
			lr.Filter.Prefix = aws.ToString(rule.Prefix)
		}

		// Transitions
		for _, t := range rule.Transitions {
			lr.Transitions = append(lr.Transitions, LifecycleTransition{
				Days:         aws.ToInt32(t.Days),
				StorageClass: string(t.StorageClass),
			})
		}

		// Expiration
		if rule.Expiration != nil {
			exp := &LifecycleExpiration{
				Days: aws.ToInt32(rule.Expiration.Days),
				Date: rule.Expiration.Date,
			}
			lr.Expiration = exp
		}

		// NoncurrentVersion
		if rule.NoncurrentVersionExpiration != nil {
			lr.NoncurrentVersion = &NoncurrentVersionExpiration{
				Days: aws.ToInt32(rule.NoncurrentVersionExpiration.NoncurrentDays),
			}
		}

		cfg.Lifecycle = append(cfg.Lifecycle, lr)
	}

	return nil
}

func extractVersioning(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketVersioning(ctx, &s3.GetBucketVersioningInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	cfg.Versioning.Status = string(resp.Status)
	cfg.Versioning.MFADelete = string(resp.MFADelete)
	return nil
}

func extractCORS(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketCors(ctx, &s3.GetBucketCorsInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	for _, rule := range resp.CORSRules {
		cr := CORSRule{
			AllowedHeaders: rule.AllowedHeaders,
			AllowedMethods: rule.AllowedMethods,
			AllowedOrigins: rule.AllowedOrigins,
			ExposeHeaders:  rule.ExposeHeaders,
			MaxAgeSeconds:  aws.ToInt32(rule.MaxAgeSeconds),
		}
		cfg.CORS = append(cfg.CORS, cr)
	}

	return nil
}

func extractLogging(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketLogging(ctx, &s3.GetBucketLoggingInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	if resp.LoggingEnabled != nil {
		cfg.Logging.TargetBucket = aws.ToString(resp.LoggingEnabled.TargetBucket)
		cfg.Logging.TargetPrefix = aws.ToString(resp.LoggingEnabled.TargetPrefix)
	}

	return nil
}

func extractPolicy(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketPolicy(ctx, &s3.GetBucketPolicyInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	if resp.Policy != nil {
		cfg.Policy = aws.ToString(resp.Policy)
	}

	return nil
}

func extractWebsite(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketWebsite(ctx, &s3.GetBucketWebsiteInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	wc := &WebsiteConfig{}
	if resp.IndexDocument != nil {
		wc.IndexDocument = aws.ToString(resp.IndexDocument.Suffix)
	}
	if resp.ErrorDocument != nil {
		wc.ErrorDocument = aws.ToString(resp.ErrorDocument.Key)
	}
	if resp.RedirectAllRequestsTo != nil {
		wc.RedirectAll = aws.ToString(resp.RedirectAllRequestsTo.HostName)
	}
	cfg.Website = wc

	return nil
}

func extractReplication(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketReplication(ctx, &s3.GetBucketReplicationInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	if resp.ReplicationConfiguration != nil {
		rc := &ReplicationConfig{
			Role: aws.ToString(resp.ReplicationConfiguration.Role),
		}
		for _, rule := range resp.ReplicationConfiguration.Rules {
			dest := ""
			if rule.Destination != nil {
				dest = aws.ToString(rule.Destination.Bucket)
			}
			prefix := ""
			if rule.Filter != nil {
				if f, ok := rule.Filter.(*types.ReplicationRuleFilterMemberPrefix); ok {
					prefix = f.Value
				}
			}
			if rule.Prefix != nil {
				prefix = aws.ToString(rule.Prefix)
			}
			rc.Rules = append(rc.Rules, ReplicationRule{
				ID:          aws.ToString(rule.ID),
				Status:      string(rule.Status),
				Destination: dest,
				Prefix:      prefix,
			})
		}
		cfg.Replication = rc
	}

	return nil
}

func extractACL(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketAcl(ctx, &s3.GetBucketAclInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	// Determina a ACL canned baseada nas permissões
	cfg.ACL.CannedACL = "private" // padrão

	for _, grant := range resp.Grants {
		if grant.Grantee == nil {
			continue
		}
		if grant.Grantee.Type == types.TypeGroup &&
			grant.Grantee.URI != nil &&
			strings.Contains(aws.ToString(grant.Grantee.URI), "AllUsers") {
			if grant.Permission == types.PermissionRead {
				cfg.ACL.CannedACL = "public-read"
			} else if grant.Permission == types.PermissionFullControl {
				cfg.ACL.CannedACL = "public-read-write"
			}
			break
		}
	}

	return nil
}

func extractPublicAccessBlock(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetPublicAccessBlock(ctx, &s3.GetPublicAccessBlockInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	if resp.PublicAccessBlockConfiguration != nil {
		pab := resp.PublicAccessBlockConfiguration
		cfg.PublicAccessBlock = PublicAccessBlock{
			BlockPublicAcls:       aws.ToBool(pab.BlockPublicAcls),
			IgnorePublicAcls:      aws.ToBool(pab.IgnorePublicAcls),
			BlockPublicPolicy:     aws.ToBool(pab.BlockPublicPolicy),
			RestrictPublicBuckets: aws.ToBool(pab.RestrictPublicBuckets),
		}
	}

	return nil
}

func extractObjectLock(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetObjectLockConfiguration(ctx, &s3.GetObjectLockConfigurationInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		return err
	}

	if resp.ObjectLockConfiguration != nil {
		cfg.ObjectLock.Enabled = resp.ObjectLockConfiguration.ObjectLockEnabled == types.ObjectLockEnabledEnabled

		if resp.ObjectLockConfiguration.Rule != nil && resp.ObjectLockConfiguration.Rule.DefaultRetention != nil {
			dr := resp.ObjectLockConfiguration.Rule.DefaultRetention
			cfg.ObjectLock.Mode = string(dr.Mode)
			cfg.ObjectLock.RetentionDays = aws.ToInt32(dr.Days)
			cfg.ObjectLock.RetentionYears = aws.ToInt32(dr.Years)
		}
	}

	return nil
}

func extractTags(ctx context.Context, client *s3.Client, cfg *BucketConfig) error {
	resp, err := client.GetBucketTagging(ctx, &s3.GetBucketTaggingInput{
		Bucket: aws.String(cfg.BucketName),
	})
	if err != nil {
		if isNoSuchTagSet(err) {
			return nil
		}
		return err
	}

	for _, tag := range resp.TagSet {
		cfg.Tags = append(cfg.Tags, Tag{
			Key:   aws.ToString(tag.Key),
			Value: aws.ToString(tag.Value),
		})
	}

	return nil
}

// SaveBucketConfig salva a configuração do bucket em um arquivo JSON.
func SaveBucketConfig(cfg *BucketConfig, outputDir string) (string, error) {
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		return "", fmt.Errorf("falha ao criar diretório %q: %w", outputDir, err)
	}

	outputPath := filepath.Join(outputDir, ".s3config.json")

	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return "", fmt.Errorf("falha ao serializar config: %w", err)
	}

	if err := os.WriteFile(outputPath, data, 0644); err != nil {
		return "", fmt.Errorf("falha ao escrever %q: %w", outputPath, err)
	}

	return outputPath, nil
}

// LoadBucketConfig carrega a configuração do bucket de um arquivo JSON.
func LoadBucketConfig(path string) (*BucketConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("falha ao ler %q: %w", path, err)
	}

	var cfg BucketConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("falha ao deserializar config: %w", err)
	}

	return &cfg, nil
}

// --- helpers para detecção de erros específicos da AWS ---

func isNoSuchLifecycleConfig(err error) bool {
	return err != nil && strings.Contains(err.Error(), "NoSuchLifecycleConfiguration")
}

func isNoSuchCORSConfig(err error) bool {
	return err != nil && strings.Contains(err.Error(), "NoSuchCORSConfiguration")
}

func isNoSuchBucketPolicy(err error) bool {
	return err != nil && strings.Contains(err.Error(), "NoSuchBucketPolicy")
}

func isNoSuchWebsiteConfig(err error) bool {
	return err != nil && (strings.Contains(err.Error(), "NoSuchWebsiteConfiguration") ||
		strings.Contains(err.Error(), "NoSuchWebsite"))
}

func isReplicationNotFound(err error) bool {
	return err != nil && (strings.Contains(err.Error(), "ReplicationConfigurationNotFoundError") ||
		strings.Contains(err.Error(), "NoSuchReplicationConfiguration"))
}

func isObjectLockNotEnabled(err error) bool {
	return err != nil && (strings.Contains(err.Error(), "ObjectLockConfigurationNotFoundError") ||
		strings.Contains(err.Error(), "NoSuchObjectLockConfiguration"))
}

func isNoSuchTagSet(err error) bool {
	return err != nil && strings.Contains(err.Error(), "NoSuchTagSet")
}

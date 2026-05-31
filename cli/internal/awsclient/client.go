package awsclient

import (
	"context"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
)

// Options configures how the AWS client is created.
type Options struct {
	Profile string
	Region  string
}

// New creates an aws.Config with the given profile and region.
// If profile is empty, the default credential chain is used.
func New(ctx context.Context, opts Options) (aws.Config, error) {
	loadOpts := []func(*config.LoadOptions) error{
		config.WithRegion(opts.Region),
	}

	if opts.Profile != "" {
		loadOpts = append(loadOpts, config.WithSharedConfigProfile(opts.Profile))
	}

	cfg, err := config.LoadDefaultConfig(ctx, loadOpts...)
	if err != nil {
		return aws.Config{}, fmt.Errorf("falha ao carregar configuração AWS: %w", err)
	}

	return cfg, nil
}

// NewWithStaticCredentials cria um aws.Config com credenciais estáticas.
// Útil para testes e ambientes onde as credenciais são passadas explicitamente.
func NewWithStaticCredentials(ctx context.Context, region, accessKey, secretKey, sessionToken string) (aws.Config, error) {
	cfg, err := config.LoadDefaultConfig(ctx,
		config.WithRegion(region),
		config.WithCredentialsProvider(credentials.NewStaticCredentialsProvider(
			accessKey, secretKey, sessionToken,
		)),
	)
	if err != nil {
		return aws.Config{}, fmt.Errorf("falha ao criar configuração AWS com credenciais estáticas: %w", err)
	}

	return cfg, nil
}

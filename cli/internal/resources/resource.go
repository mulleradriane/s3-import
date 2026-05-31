package resources

import (
	"context"

	"github.com/aws/aws-sdk-go-v2/aws"
)

// DiscoverOpts holds options for the discover command.
type DiscoverOpts struct {
	CSVPath string
	Prefix  string
	Env     string
}

// MigrateOpts holds options for the migrate command.
type MigrateOpts struct {
	Team       string
	Env        string
	AssetCat   string
	OutputDir  string
	DryRun     bool
	SkipImport bool
	SkipPlan   bool
}

// DiscoveryResult holds the result of discovering a single resource.
type DiscoveryResult struct {
	Name   string
	Tier   string // AUTO, REVIEW, BLOCK
	Reason string
	Extra  map[string]string
}

// Resource is the interface that every AWS resource type must implement.
// This allows the CLI to be extended to support new resource types (apigw, sqs, sns, etc.)
// without changing the core command infrastructure.
type Resource interface {
	// Name returns the resource type identifier, e.g. "s3", "apigw".
	Name() string

	// Discover reads a CSV file and classifies each resource into a tier.
	Discover(ctx context.Context, cfg aws.Config, opts DiscoverOpts) ([]DiscoveryResult, error)

	// Extract fetches the current configuration of a named resource from AWS.
	// Returns an opaque interface{} which is the resource-specific config struct.
	Extract(ctx context.Context, cfg aws.Config, name string) (interface{}, error)

	// Generate produces Terraform files (main.tf, backend.tf, CHANGES.md) from
	// the extracted configuration.
	Generate(ctx context.Context, extracted interface{}, opts MigrateOpts) error

	// Migrate orchestrates the full pipeline:
	// preflight → extract → generate → tf init → import → plan
	Migrate(ctx context.Context, cfg aws.Config, name string, opts MigrateOpts) error
}

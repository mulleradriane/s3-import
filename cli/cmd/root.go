package cmd

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

// GlobalFlags holds the persistent flags shared across all commands.
type GlobalFlags struct {
	Profile string
	Region  string
	Output  string
	DryRun  bool
}

var globalFlags GlobalFlags

var rootCmd = &cobra.Command{
	Use:   "migration-cli",
	Short: "CLI para migrar recursos AWS para Terraform Blueprint interno",
	Long: `migration-cli é uma ferramenta para migrar recursos AWS (S3, API Gateway, SQS, SNS)
para o padrão interno de Blueprint Terraform (BP).

A migração é feita por ondas (dev → hml → prd) por time/produto.
Filosofia: buckets são recursos de produção — o pipeline informa e só muda
ativamente regras de lifecycle.`,
	Version: "1.0.0",
}

// Execute is the entry point for the CLI.
func Execute() {
	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func init() {
	rootCmd.PersistentFlags().StringVar(&globalFlags.Profile, "profile", "", "AWS profile para usar (ex: default, prod-admin)")
	rootCmd.PersistentFlags().StringVar(&globalFlags.Region, "region", "us-east-1", "AWS region")
	rootCmd.PersistentFlags().StringVar(&globalFlags.Output, "output", "text", "Formato de saída: text | json | table")
	rootCmd.PersistentFlags().BoolVar(&globalFlags.DryRun, "dry-run", false, "Executa sem fazer alterações reais")
}

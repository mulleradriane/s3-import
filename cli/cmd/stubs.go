package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

// apigwCmd represents the apigw group
var apigwCmd = &cobra.Command{
	Use:   "apigw",
	Short: "Migração de API Gateways para Blueprint Terraform",
	Long:  `Grupo de comandos para migrar API Gateway resources para o padrão BP.`,
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso API Gateway em desenvolvimento, disponível em breve.")
		fmt.Println("   Acompanhe as releases em: https://github.com/mulleradriane/migration-cli/releases")
	},
}

// sqsCmd represents the sqs group
var sqsCmd = &cobra.Command{
	Use:   "sqs",
	Short: "Migração de SQS Queues para Blueprint Terraform",
	Long:  `Grupo de comandos para migrar SQS queues para o padrão BP.`,
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SQS em desenvolvimento, disponível em breve.")
		fmt.Println("   Acompanhe as releases em: https://github.com/mulleradriane/migration-cli/releases")
	},
}

// snsCmd represents the sns group
var snsCmd = &cobra.Command{
	Use:   "sns",
	Short: "Migração de SNS Topics para Blueprint Terraform",
	Long:  `Grupo de comandos para migrar SNS topics para o padrão BP.`,
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SNS em desenvolvimento, disponível em breve.")
		fmt.Println("   Acompanhe as releases em: https://github.com/mulleradriane/migration-cli/releases")
	},
}

// apigwDiscoverCmd é stub para descoberta de API Gateways
var apigwDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre API Gateways (em breve)",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso API Gateway em desenvolvimento, disponível em breve.")
	},
}

// sqsDiscoverCmd é stub para descoberta de SQS
var sqsDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre SQS Queues (em breve)",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SQS em desenvolvimento, disponível em breve.")
	},
}

// snsDiscoverCmd é stub para descoberta de SNS
var snsDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre SNS Topics (em breve)",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SNS em desenvolvimento, disponível em breve.")
	},
}

func init() {
	apigwCmd.AddCommand(apigwDiscoverCmd)
	sqsCmd.AddCommand(sqsDiscoverCmd)
	snsCmd.AddCommand(snsDiscoverCmd)

	rootCmd.AddCommand(apigwCmd)
	rootCmd.AddCommand(sqsCmd)
	rootCmd.AddCommand(snsCmd)
}

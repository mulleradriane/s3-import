package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

// sqsCmd represents the sqs group
var sqsCmd = &cobra.Command{
	Use:   "sqs",
	Short: "Migração de SQS Queues para Blueprint Terraform",
	Long:  `Grupo de comandos para migrar SQS queues para o padrão BP.`,
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SQS em desenvolvimento, disponível em breve.")
	},
}

// snsCmd represents the sns group
var snsCmd = &cobra.Command{
	Use:   "sns",
	Short: "Migração de SNS Topics para Blueprint Terraform",
	Long:  `Grupo de comandos para migrar SNS topics para o padrão BP.`,
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SNS em desenvolvimento, disponível em breve.")
	},
}

var sqsDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre SQS Queues (em breve)",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SQS em desenvolvimento, disponível em breve.")
	},
}

var snsDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre SNS Topics (em breve)",
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Println("⚠️  Recurso SNS em desenvolvimento, disponível em breve.")
	},
}

func init() {
	sqsCmd.AddCommand(sqsDiscoverCmd)
	snsCmd.AddCommand(snsDiscoverCmd)

	rootCmd.AddCommand(sqsCmd)
	rootCmd.AddCommand(snsCmd)
}

package cmd

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"

	"github.com/mulleradriane/migration-cli/internal/awsclient"
	"github.com/mulleradriane/migration-cli/internal/output"
	"github.com/mulleradriane/migration-cli/internal/resources"
	"github.com/mulleradriane/migration-cli/internal/resources/apigw"
)

// apigwRootCmd é o grupo de comandos API Gateway
var apigwRootCmd = &cobra.Command{
	Use:   "apigw",
	Short: "Migração de API Gateways para Blueprint Terraform",
	Long: `Grupo de comandos para descobrir, extrair, gerar e migrar
REST APIs do API Gateway para o padrão interno de Blueprint Terraform (BP).

Fluxo típico de migração:
  1. migration-cli apigw discover --prefix apigw-
  2. migration-cli apigw preflight --api-id API_ID
  3. migration-cli apigw migrate --api-id API_ID --team myteam`,
}

// --- apigw discover ---

var apigwDiscoverFlags struct {
	prefix string
	env    string
	tier   string
}

var apigwDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre e classifica APIs REST do API Gateway",
	Long: `Conecta na AWS e lista todas as REST APIs do API Gateway,
classificando cada uma em um tier:
  AUTO             — pode ser importado diretamente
  REVIEW_VPCLINK   — usa VPC Link, requer revisão de naming
  REVIEW_HARDCODED — URI com ambiente hardcoded (dev/hml/prd)
  REVIEW_LIMIT     — próximo do limite de 500 resources
  BLOCK            — nome duplicado ou outro bloqueador`,
	Example: `  migration-cli apigw discover
  migration-cli apigw discover --prefix apigw-
  migration-cli apigw discover --env prd --tier AUTO --output table`,
	RunE: func(cmd *cobra.Command, args []string) error {
		ctx := context.Background()

		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			return fmt.Errorf("falha ao criar AWS client: %w", err)
		}

		printer := output.New(output.Format(globalFlags.Output))
		printer.PrintSection("Descobrindo APIs REST...")

		results, err := apigw.Discover(ctx, awsCfg, apigw.DiscoverOpts{
			Prefix: apigwDiscoverFlags.prefix,
			Env:    apigwDiscoverFlags.env,
			Tier:   apigwDiscoverFlags.tier,
		})
		if err != nil {
			return fmt.Errorf("discover falhou: %w", err)
		}

		if len(results) == 0 {
			fmt.Println("Nenhuma API encontrada com os filtros especificados.")
			return nil
		}

		var rows []output.DiscoveryRow
		for _, r := range results {
			rows = append(rows, output.DiscoveryRow{
				Name:   r.Name,
				Tier:   r.Tier,
				Reason: r.Reason,
				Team:   r.Team,
				Env:    r.Env,
			})
		}

		printer.PrintDiscoveryResults(rows)
		return nil
	},
}

// --- apigw extract ---

var apigwExtractFlags struct {
	apiID     string
	outputDir string
}

var apigwExtractCmd = &cobra.Command{
	Use:   "extract",
	Short: "Extrai a configuração atual de uma API Gateway para .apigwconfig.json",
	Long: `Conecta na AWS e extrai todas as configurações relevantes da API:
  - Info base (nome, endpoint type, policy)
  - Stages (com certificados e throttling)
  - Authorizers
  - Usage plans associados
  - Base path mappings de domínios customizados
  - Export OAS3 (openapi.json)
  - Integrações (para detecção de VPC Link e URI hardcoded)

Salva o resultado em .apigwconfig.json e openapi.json no diretório especificado.`,
	Example: `  migration-cli apigw extract --api-id abc123def
  migration-cli apigw extract --api-id abc123def --output-dir ./output/minha-api`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if apigwExtractFlags.apiID == "" {
			return fmt.Errorf("--api-id é obrigatório")
		}

		ctx := context.Background()
		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			return fmt.Errorf("falha ao criar AWS client: %w", err)
		}

		outputDir := apigwExtractFlags.outputDir
		if outputDir == "" {
			outputDir = filepath.Join(".", apigwExtractFlags.apiID)
		}

		printer := output.New(output.Format(globalFlags.Output))
		printer.PrintSection(fmt.Sprintf("Extraindo configuração: %s", apigwExtractFlags.apiID))

		apicfg, err := apigw.Extract(ctx, awsCfg, apigwExtractFlags.apiID)
		if err != nil {
			return fmt.Errorf("extração falhou: %w", err)
		}

		if err := apigw.SaveAPIConfig(apicfg, outputDir); err != nil {
			return fmt.Errorf("falha ao salvar config: %w", err)
		}

		printer.PrintSuccess(fmt.Sprintf("Config salva em: %s/.apigwconfig.json", outputDir))
		printer.PrintKeyValue("Nome", apicfg.Name)
		printer.PrintKeyValue("Produto", apicfg.Product)
		printer.PrintKeyValue("Endpoint Type", apicfg.EndpointType)
		printer.PrintKeyValue("Pattern", string(apicfg.Pattern))
		printer.PrintKeyValue("Tier", string(apicfg.Tier))
		printer.PrintKeyValue("Stages", fmt.Sprintf("%d stage(s)", len(apicfg.Stages)))
		printer.PrintKeyValue("Resources", fmt.Sprintf("%d resource(s)", apicfg.ResourceCount))
		printer.PrintKeyValue("Authorizers", fmt.Sprintf("%d authorizer(s)", len(apicfg.Authorizers)))
		printer.PrintKeyValue("Usage Plans", fmt.Sprintf("%d usage plan(s)", len(apicfg.UsagePlans)))
		printer.PrintKeyValue("Base Path Mappings", fmt.Sprintf("%d mapeamento(s)", len(apicfg.BasePathMappings)))

		if len(apicfg.OAS3Export) > 0 {
			printer.PrintKeyValue("OAS3 Export", fmt.Sprintf("%d bytes → openapi.json", len(apicfg.OAS3Export)))
		} else {
			printer.PrintWarning("OAS3 Export não disponível")
		}

		return nil
	},
}

// --- apigw generate ---

var apigwGenerateFlags struct {
	apiID     string
	team      string
	env       string
	outputDir string
}

var apigwGenerateCmd = &cobra.Command{
	Use:   "generate",
	Short: "Gera arquivos Terraform para uma API Gateway",
	Long: `Lê o .apigwconfig.json (gerado pelo comando extract) e gera:
  - openapi.json  — export OAS3 da API (cópia exata do estado atual)
  - main.tf       — módulo BP com a configuração da API
  - backend.tf    — backend S3 no padrão da empresa
  - CHANGES.md    — documentação do que foi encontrado e próximos passos
  - _import_commands.sh — comandos terraform import (não vai pro commit)

Se .apigwconfig.json não existir, faz o extract automaticamente.`,
	Example: `  migration-cli apigw generate --api-id abc123def --team myteam --env prd
  migration-cli apigw generate --api-id abc123def --team myteam --output-dir ./tf`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if apigwGenerateFlags.apiID == "" {
			return fmt.Errorf("--api-id é obrigatório")
		}
		if apigwGenerateFlags.team == "" {
			return fmt.Errorf("--team é obrigatório")
		}

		ctx := context.Background()

		outputDir := apigwGenerateFlags.outputDir
		if outputDir == "" {
			outputDir = filepath.Join(".", apigwGenerateFlags.apiID)
		}

		printer := output.New(output.Format(globalFlags.Output))
		printer.PrintSection(fmt.Sprintf("Gerando Terraform: %s", apigwGenerateFlags.apiID))

		// Tenta carregar .apigwconfig.json existente
		configPath := filepath.Join(outputDir, ".apigwconfig.json")
		var apicfg *apigw.APIConfig

		if _, err := os.Stat(configPath); err == nil {
			printer.PrintInfo(fmt.Sprintf("Carregando config existente: %s", configPath))
			apicfg, err = apigw.LoadAPIConfig(configPath)
			if err != nil {
				return fmt.Errorf("falha ao carregar config: %w", err)
			}
		} else {
			// Faz o extract automaticamente
			printer.PrintInfo("Config não encontrada — executando extract...")
			awsCfg, err := awsclient.New(ctx, awsclient.Options{
				Profile: globalFlags.Profile,
				Region:  globalFlags.Region,
			})
			if err != nil {
				return fmt.Errorf("falha ao criar AWS client: %w", err)
			}

			apicfg, err = apigw.Extract(ctx, awsCfg, apigwGenerateFlags.apiID)
			if err != nil {
				return fmt.Errorf("extração falhou: %w", err)
			}
		}

		// Enriquece com metadados
		apicfg.Team = apigwGenerateFlags.team
		if apigwGenerateFlags.env != "" {
			apicfg.Env = apigwGenerateFlags.env
		}

		printer.PrintKeyValue("Tier", string(apicfg.Tier))
		printer.PrintKeyValue("Pattern", string(apicfg.Pattern))

		if apicfg.Tier == apigw.TierBLOCK {
			printer.PrintError("Tier BLOCK — gerando apenas CHANGES.md com os bloqueadores")
		}

		if err := apigw.GenerateFiles(apicfg, outputDir); err != nil {
			return fmt.Errorf("geração falhou: %w", err)
		}

		printer.PrintSuccess(fmt.Sprintf("Arquivos gerados em %s/", outputDir))
		fmt.Printf("  * %s/openapi.json\n", outputDir)
		fmt.Printf("  * %s/main.tf\n", outputDir)
		fmt.Printf("  * %s/backend.tf\n", outputDir)
		fmt.Printf("  * %s/CHANGES.md\n", outputDir)
		fmt.Printf("  * %s/_import_commands.sh\n", outputDir)

		if apicfg.Tier != apigw.TierAUTO {
			printer.PrintWarning("Revise o CHANGES.md antes de aplicar o Terraform")
		}

		return nil
	},
}

// --- apigw preflight ---

var apigwPreflightFlags struct {
	apiID string
}

var apigwPreflightCmd = &cobra.Command{
	Use:   "preflight",
	Short: "Verifica bloqueadores em uma API Gateway sem gerar nada",
	Long: `Executa apenas a verificação de pré-voo (preflight) na API:
  - Nome duplicado (BLOCK)
  - Resource count próximo do limite de 500 (REVIEW)

Não gera nenhum arquivo — apenas informa os problemas encontrados.`,
	Example: `  migration-cli apigw preflight --api-id abc123def
  migration-cli apigw preflight --api-id abc123def --profile prod-admin`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if apigwPreflightFlags.apiID == "" {
			return fmt.Errorf("--api-id é obrigatório")
		}

		ctx := context.Background()
		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			return fmt.Errorf("falha ao criar AWS client: %w", err)
		}

		printer := output.New(output.Format(globalFlags.Output))

		issues, err := apigw.Preflight(ctx, awsCfg, apigwPreflightFlags.apiID)
		if err != nil {
			return fmt.Errorf("preflight falhou: %w", err)
		}

		// Converte para o tipo da camada de output
		var outputIssues []output.PreflightIssue
		for _, i := range issues {
			outputIssues = append(outputIssues, output.PreflightIssue{
				Severity: i.Severity,
				Code:     i.Code,
				Message:  i.Message,
			})
		}

		printer.PrintPreflightResults(apigwPreflightFlags.apiID, outputIssues)

		// Retorna código de saída não-zero se houver bloqueadores
		for _, i := range issues {
			if i.Severity == "block" {
				os.Exit(1)
			}
		}

		return nil
	},
}

// --- apigw migrate ---

var apigwMigrateFlags struct {
	apiID      string
	team       string
	env        string
	outputDir  string
	skipImport bool
	skipPlan   bool
}

var apigwMigrateCmd = &cobra.Command{
	Use:   "migrate",
	Short: "Pipeline completo: preflight → extract → generate → terraform init → import → plan",
	Long: `Executa o pipeline completo de migração de uma API Gateway:

  1. Preflight  — verifica bloqueadores (nome duplicado, resource limit)
  2. Extract    — extrai configuração atual da API via AWS APIs
  3. Classify   — classifica o tier (AUTO/REVIEW_*/BLOCK)
  4. Generate   — gera openapi.json, main.tf, backend.tf, CHANGES.md, _import_commands.sh
  5. TF Init    — terraform init
  6. TF Import  — importa a API e stages para o estado Terraform
  7. TF Plan    — gera o plano de execução (deve mostrar zero diff)

Flag --dry-run: executa etapas 1-4 sem chamar Terraform.`,
	Example: `  migration-cli apigw migrate --api-id abc123def --team myteam
  migration-cli apigw migrate --api-id abc123def --team myteam --env prd
  migration-cli apigw migrate --api-id abc123def --team myteam --dry-run
  migration-cli apigw migrate --api-id abc123def --team myteam --skip-import`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if apigwMigrateFlags.apiID == "" {
			return fmt.Errorf("--api-id é obrigatório")
		}
		if apigwMigrateFlags.team == "" {
			return fmt.Errorf("--team é obrigatório")
		}

		ctx := context.Background()
		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			return fmt.Errorf("falha ao criar AWS client: %w", err)
		}

		outputDir := apigwMigrateFlags.outputDir
		if outputDir == "" {
			outputDir = filepath.Join(".", "output", apigwMigrateFlags.apiID)
		}

		resource := apigw.New()
		return resource.Migrate(ctx, awsCfg, apigwMigrateFlags.apiID, resources.MigrateOpts{
			Team:       apigwMigrateFlags.team,
			Env:        apigwMigrateFlags.env,
			OutputDir:  outputDir,
			DryRun:     globalFlags.DryRun,
			SkipImport: apigwMigrateFlags.skipImport,
			SkipPlan:   apigwMigrateFlags.skipPlan,
		})
	},
}

func init() {
	// discover flags
	apigwDiscoverCmd.Flags().StringVar(&apigwDiscoverFlags.prefix, "prefix", "", "Filtra APIs pelo prefixo do nome (ex: apigw-)")
	apigwDiscoverCmd.Flags().StringVar(&apigwDiscoverFlags.env, "env", "", "Filtra por ambiente detectado (dev, hml, prd) — Pattern B apenas")
	apigwDiscoverCmd.Flags().StringVar(&apigwDiscoverFlags.tier, "tier", "", "Filtra por tier (AUTO, REVIEW_VPCLINK, REVIEW_HARDCODED, REVIEW_LIMIT, BLOCK)")

	// extract flags
	apigwExtractCmd.Flags().StringVar(&apigwExtractFlags.apiID, "api-id", "", "ID da API Gateway (obrigatório)")
	apigwExtractCmd.Flags().StringVar(&apigwExtractFlags.outputDir, "output-dir", "", "Diretório de saída (padrão: ./<api-id>)")

	// generate flags
	apigwGenerateCmd.Flags().StringVar(&apigwGenerateFlags.apiID, "api-id", "", "ID da API Gateway (obrigatório)")
	apigwGenerateCmd.Flags().StringVar(&apigwGenerateFlags.team, "team", "", "Time responsável (obrigatório)")
	apigwGenerateCmd.Flags().StringVar(&apigwGenerateFlags.env, "env", "", "Ambiente: dev, hml, prd (opcional — sobrescreve o detectado no nome)")
	apigwGenerateCmd.Flags().StringVar(&apigwGenerateFlags.outputDir, "output-dir", "", "Diretório de saída (padrão: ./<api-id>)")

	// preflight flags
	apigwPreflightCmd.Flags().StringVar(&apigwPreflightFlags.apiID, "api-id", "", "ID da API Gateway (obrigatório)")

	// migrate flags
	apigwMigrateCmd.Flags().StringVar(&apigwMigrateFlags.apiID, "api-id", "", "ID da API Gateway (obrigatório)")
	apigwMigrateCmd.Flags().StringVar(&apigwMigrateFlags.team, "team", "", "Time responsável (obrigatório)")
	apigwMigrateCmd.Flags().StringVar(&apigwMigrateFlags.env, "env", "", "Ambiente: dev, hml, prd (opcional)")
	apigwMigrateCmd.Flags().StringVar(&apigwMigrateFlags.outputDir, "output-dir", "", "Diretório de saída (padrão: ./output/<api-id>)")
	apigwMigrateCmd.Flags().BoolVar(&apigwMigrateFlags.skipImport, "skip-import", false, "Pula o terraform import")
	apigwMigrateCmd.Flags().BoolVar(&apigwMigrateFlags.skipPlan, "skip-plan", false, "Pula o terraform plan")

	// Adiciona subcomandos ao grupo apigw
	apigwRootCmd.AddCommand(apigwDiscoverCmd)
	apigwRootCmd.AddCommand(apigwExtractCmd)
	apigwRootCmd.AddCommand(apigwGenerateCmd)
	apigwRootCmd.AddCommand(apigwPreflightCmd)
	apigwRootCmd.AddCommand(apigwMigrateCmd)

	// Registra o grupo apigw no root
	rootCmd.AddCommand(apigwRootCmd)
}

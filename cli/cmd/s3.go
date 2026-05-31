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
	"github.com/mulleradriane/migration-cli/internal/resources/s3"
)

// s3Cmd é o grupo de comandos S3
var s3Cmd = &cobra.Command{
	Use:   "s3",
	Short: "Migração de S3 Buckets para Blueprint Terraform",
	Long: `Grupo de comandos para descobrir, extrair, gerar e migrar
S3 buckets para o padrão interno de Blueprint Terraform (BP).

Fluxo típico de migração:
  1. migration-cli s3 discover --csv levantamento.csv
  2. migration-cli s3 preflight --bucket meu-bucket
  3. migration-cli s3 migrate --bucket meu-bucket --team myteam --env dev --asset-cat Logs`,
}

// --- s3 discover ---

var discoverFlags struct {
	csvPath string
	prefix  string
	env     string
}

var s3DiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Descobre e classifica buckets S3 a partir de um CSV",
	Long: `Lê um CSV de levantamento e classifica cada bucket em um tier:
  AUTO   — pode ser migrado automaticamente
  REVIEW — pode ser migrado mas requer revisão manual
  BLOCK  — tem bloqueadores, não pode ser migrado

O CSV deve ter as colunas: bucket_name, team, env, asset_category, category, blockers`,
	Example: `  migration-cli s3 discover --csv levantamento.csv
  migration-cli s3 discover --csv levantamento.csv --env dev
  migration-cli s3 discover --csv levantamento.csv --prefix myteam- --output table`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if discoverFlags.csvPath == "" {
			return fmt.Errorf("--csv é obrigatório")
		}

		ctx := context.Background()

		// Cria AWS config (discover não precisa de AWS, mas mantemos a interface)
		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			// Em discover, podemos continuar sem AWS (só analisa o CSV)
			fmt.Fprintf(os.Stderr, "⚠️  Aviso: sem conexão AWS (apenas análise CSV): %v\n", err)
		}

		resource := s3.New()
		results, err := resource.Discover(ctx, awsCfg, resources.DiscoverOpts{
			CSVPath: discoverFlags.csvPath,
			Prefix:  discoverFlags.prefix,
			Env:     discoverFlags.env,
		})
		if err != nil {
			return fmt.Errorf("discover falhou: %w", err)
		}

		if len(results) == 0 {
			fmt.Println("Nenhum bucket encontrado com os filtros especificados.")
			return nil
		}

		printer := output.New(output.Format(globalFlags.Output))

		var rows []output.DiscoveryRow
		for _, r := range results {
			rows = append(rows, output.DiscoveryRow{
				Name:   r.Name,
				Tier:   r.Tier,
				Reason: r.Reason,
				Team:   r.Extra["team"],
				Env:    r.Extra["env"],
			})
		}

		printer.PrintDiscoveryResults(rows)
		return nil
	},
}

// --- s3 extract ---

var extractFlags struct {
	bucket    string
	outputDir string
}

var s3ExtractCmd = &cobra.Command{
	Use:   "extract",
	Short: "Extrai a configuração atual de um bucket S3 para .s3config.json",
	Long: `Conecta na AWS e extrai todas as configurações relevantes do bucket:
  - Encryption, Lifecycle, Versioning, CORS, Logging
  - Bucket Policy, Website, Replication, ACL, Tags, Object Lock

Salva o resultado em .s3config.json no diretório especificado.`,
	Example: `  migration-cli s3 extract --bucket meu-bucket
  migration-cli s3 extract --bucket meu-bucket --output-dir ./output/meu-bucket`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if extractFlags.bucket == "" {
			return fmt.Errorf("--bucket é obrigatório")
		}

		ctx := context.Background()
		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			return fmt.Errorf("falha ao criar AWS client: %w", err)
		}

		outputDir := extractFlags.outputDir
		if outputDir == "" {
			outputDir = filepath.Join(".", extractFlags.bucket)
		}

		printer := output.New(output.Format(globalFlags.Output))
		printer.PrintSection(fmt.Sprintf("Extraindo configuração: %s", extractFlags.bucket))

		resource := s3.New()
		extracted, err := resource.Extract(ctx, awsCfg, extractFlags.bucket)
		if err != nil {
			return fmt.Errorf("extração falhou: %w", err)
		}

		bucketCfg := extracted.(*s3.BucketConfig)

		configPath, err := s3.SaveBucketConfig(bucketCfg, outputDir)
		if err != nil {
			return fmt.Errorf("falha ao salvar config: %w", err)
		}

		printer.PrintSuccess(fmt.Sprintf("Config salva em: %s", configPath))

		// Exibe resumo
		printer.PrintKeyValue("Region", bucketCfg.Region)
		printer.PrintKeyValue("Encryption", fmt.Sprintf("%s (%s)", bucketCfg.Encryption.Algorithm, boolStr(bucketCfg.Encryption.Enabled)))
		printer.PrintKeyValue("Versioning", bucketCfg.Versioning.Status)
		printer.PrintKeyValue("Lifecycle rules", fmt.Sprintf("%d regra(s)", len(bucketCfg.Lifecycle)))
		printer.PrintKeyValue("CORS rules", fmt.Sprintf("%d regra(s)", len(bucketCfg.CORS)))
		printer.PrintKeyValue("Logging", boolStr(bucketCfg.Logging.TargetBucket != ""))
		printer.PrintKeyValue("Replication", boolStr(bucketCfg.Replication != nil))
		printer.PrintKeyValue("Object Lock", boolStr(bucketCfg.ObjectLock.Enabled))
		printer.PrintKeyValue("ACL", bucketCfg.ACL.CannedACL)

		return nil
	},
}

// --- s3 generate ---

var generateFlags struct {
	bucket    string
	team      string
	env       string
	assetCat  string
	outputDir string
}

var s3GenerateCmd = &cobra.Command{
	Use:   "generate",
	Short: "Gera arquivos Terraform (main.tf, backend.tf, CHANGES.md) para um bucket S3",
	Long: `Lê o .s3config.json (gerado pelo comando extract) e gera:
  - main.tf    — módulo BP com a configuração do bucket
  - backend.tf — backend S3 no padrão da empresa
  - CHANGES.md — documentação do que foi encontrado e o que vai mudar

Se .s3config.json não existir no diretório, faz o extract automaticamente.`,
	Example: `  migration-cli s3 generate --bucket meu-bucket --team myteam --env dev --asset-cat Logs
  migration-cli s3 generate --bucket meu-bucket --team myteam --env dev --asset-cat "Productive data" --output-dir ./tf`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if generateFlags.bucket == "" {
			return fmt.Errorf("--bucket é obrigatório")
		}
		if generateFlags.team == "" {
			return fmt.Errorf("--team é obrigatório")
		}
		if generateFlags.env == "" {
			return fmt.Errorf("--env é obrigatório")
		}
		if generateFlags.assetCat == "" {
			return fmt.Errorf("--asset-cat é obrigatório")
		}

		ctx := context.Background()

		outputDir := generateFlags.outputDir
		if outputDir == "" {
			outputDir = filepath.Join(".", generateFlags.bucket)
		}

		printer := output.New(output.Format(globalFlags.Output))
		printer.PrintSection(fmt.Sprintf("Gerando Terraform: %s", generateFlags.bucket))

		// Tenta carregar .s3config.json existente
		configPath := filepath.Join(outputDir, ".s3config.json")
		var bucketCfg *s3.BucketConfig

		if _, err := os.Stat(configPath); err == nil {
			printer.PrintInfo(fmt.Sprintf("Carregando config existente: %s", configPath))
			bucketCfg, err = s3.LoadBucketConfig(configPath)
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

			resource := s3.New()
			extracted, err := resource.Extract(ctx, awsCfg, generateFlags.bucket)
			if err != nil {
				return fmt.Errorf("extração falhou: %w", err)
			}
			bucketCfg = extracted.(*s3.BucketConfig)
		}

		// Enriquece com metadados
		bucketCfg.Team = generateFlags.team
		bucketCfg.Env = generateFlags.env
		bucketCfg.AssetCategory = generateFlags.assetCat
		bucketCfg.BucketName = generateFlags.bucket

		// Classifica o tier
		tier, issues := s3.ClassifyTierFromConfig(bucketCfg)
		bucketCfg.Tier = tier
		bucketCfg.Issues = append(bucketCfg.Issues, issues...)

		printer.PrintKeyValue("Tier", string(tier))

		if tier == s3.TierBLOCK {
			printer.PrintError("Tier BLOCK — gerando apenas CHANGES.md com os bloqueadores")
		}

		// Gera os arquivos
		if err := s3.GenerateFiles(bucketCfg, outputDir); err != nil {
			return fmt.Errorf("geração falhou: %w", err)
		}

		printer.PrintSuccess(fmt.Sprintf("Arquivos gerados em %s/", outputDir))
		fmt.Printf("  • %s/main.tf\n", outputDir)
		fmt.Printf("  • %s/backend.tf\n", outputDir)
		fmt.Printf("  • %s/CHANGES.md\n", outputDir)

		if tier == s3.TierREVIEW {
			printer.PrintWarning("Revise o CHANGES.md antes de aplicar o Terraform")
		}

		return nil
	},
}

// --- s3 migrate ---

var migrateFlags struct {
	bucket     string
	team       string
	env        string
	assetCat   string
	outputDir  string
	skipImport bool
	skipPlan   bool
}

var s3MigrateCmd = &cobra.Command{
	Use:   "migrate",
	Short: "Pipeline completo: preflight → extract → generate → terraform init → import → plan",
	Long: `Executa o pipeline completo de migração de um bucket S3:

  1. Preflight  — verifica bloqueadores (ACL, KMS, Object Lock, Replication)
  2. Extract    — extrai configuração atual do bucket via AWS APIs
  3. Classify   — classifica o tier (AUTO/REVIEW/BLOCK)
  4. Generate   — gera main.tf, backend.tf, CHANGES.md
  5. TF Init    — terraform init
  6. TF Import  — importa o bucket para o estado Terraform
  7. TF Plan    — gera o plano de execução para revisão

Flags --dry-run: executa etapas 1-4 sem chamar Terraform.`,
	Example: `  migration-cli s3 migrate --bucket meu-bucket --team myteam --env dev --asset-cat Logs
  migration-cli s3 migrate --bucket meu-bucket --team myteam --env dev --asset-cat Logs --dry-run
  migration-cli s3 migrate --bucket meu-bucket --team myteam --env prd --asset-cat "Productive data" --skip-plan`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if migrateFlags.bucket == "" {
			return fmt.Errorf("--bucket é obrigatório")
		}
		if migrateFlags.team == "" {
			return fmt.Errorf("--team é obrigatório")
		}
		if migrateFlags.env == "" {
			return fmt.Errorf("--env é obrigatório")
		}
		if migrateFlags.assetCat == "" {
			return fmt.Errorf("--asset-cat é obrigatório")
		}

		ctx := context.Background()
		awsCfg, err := awsclient.New(ctx, awsclient.Options{
			Profile: globalFlags.Profile,
			Region:  globalFlags.Region,
		})
		if err != nil {
			return fmt.Errorf("falha ao criar AWS client: %w", err)
		}

		outputDir := migrateFlags.outputDir
		if outputDir == "" {
			outputDir = filepath.Join(".", "output", migrateFlags.bucket)
		}

		resource := s3.New()
		return resource.Migrate(ctx, awsCfg, migrateFlags.bucket, resources.MigrateOpts{
			Team:       migrateFlags.team,
			Env:        migrateFlags.env,
			AssetCat:   migrateFlags.assetCat,
			OutputDir:  outputDir,
			DryRun:     globalFlags.DryRun,
			SkipImport: migrateFlags.skipImport,
			SkipPlan:   migrateFlags.skipPlan,
		})
	},
}

// --- s3 preflight ---

var preflightFlags struct {
	bucket string
}

var s3PreflightCmd = &cobra.Command{
	Use:   "preflight",
	Short: "Verifica bloqueadores em um bucket S3 sem gerar nada",
	Long: `Executa apenas a verificação de pré-voo (preflight) no bucket:
  - ACL (deve ser private)
  - KMS key customizada
  - Object Lock habilitado
  - Replicação configurada

Não gera nenhum arquivo — apenas informa os problemas encontrados.`,
	Example: `  migration-cli s3 preflight --bucket meu-bucket
  migration-cli s3 preflight --bucket meu-bucket --profile prod-admin`,
	RunE: func(cmd *cobra.Command, args []string) error {
		if preflightFlags.bucket == "" {
			return fmt.Errorf("--bucket é obrigatório")
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

		issues, err := s3.Preflight(ctx, awsCfg, preflightFlags.bucket)
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

		printer.PrintPreflightResults(preflightFlags.bucket, outputIssues)

		// Retorna código de saída não-zero se houver bloqueadores
		for _, i := range issues {
			if i.Severity == "block" {
				os.Exit(1)
			}
		}

		return nil
	},
}

func init() {
	// discover flags
	s3DiscoverCmd.Flags().StringVar(&discoverFlags.csvPath, "csv", "", "Caminho para o CSV de levantamento (obrigatório)")
	s3DiscoverCmd.Flags().StringVar(&discoverFlags.prefix, "prefix", "", "Filtra buckets pelo prefixo")
	s3DiscoverCmd.Flags().StringVar(&discoverFlags.env, "env", "", "Filtra por ambiente (dev, hml, prd)")

	// extract flags
	s3ExtractCmd.Flags().StringVar(&extractFlags.bucket, "bucket", "", "Nome do bucket S3 (obrigatório)")
	s3ExtractCmd.Flags().StringVar(&extractFlags.outputDir, "output-dir", "", "Diretório de saída (padrão: ./<bucket>)")

	// generate flags
	s3GenerateCmd.Flags().StringVar(&generateFlags.bucket, "bucket", "", "Nome do bucket S3 (obrigatório)")
	s3GenerateCmd.Flags().StringVar(&generateFlags.team, "team", "", "Time responsável (obrigatório)")
	s3GenerateCmd.Flags().StringVar(&generateFlags.env, "env", "", "Ambiente: dev, hml, prd (obrigatório)")
	s3GenerateCmd.Flags().StringVar(&generateFlags.assetCat, "asset-cat", "", "Asset category (obrigatório)")
	s3GenerateCmd.Flags().StringVar(&generateFlags.outputDir, "output-dir", "", "Diretório de saída (padrão: ./<bucket>)")

	// migrate flags
	s3MigrateCmd.Flags().StringVar(&migrateFlags.bucket, "bucket", "", "Nome do bucket S3 (obrigatório)")
	s3MigrateCmd.Flags().StringVar(&migrateFlags.team, "team", "", "Time responsável (obrigatório)")
	s3MigrateCmd.Flags().StringVar(&migrateFlags.env, "env", "", "Ambiente: dev, hml, prd (obrigatório)")
	s3MigrateCmd.Flags().StringVar(&migrateFlags.assetCat, "asset-cat", "", "Asset category (obrigatório)")
	s3MigrateCmd.Flags().StringVar(&migrateFlags.outputDir, "output-dir", "", "Diretório de saída (padrão: ./output/<bucket>)")
	s3MigrateCmd.Flags().BoolVar(&migrateFlags.skipImport, "skip-import", false, "Pula o terraform import")
	s3MigrateCmd.Flags().BoolVar(&migrateFlags.skipPlan, "skip-plan", false, "Pula o terraform plan")

	// preflight flags
	s3PreflightCmd.Flags().StringVar(&preflightFlags.bucket, "bucket", "", "Nome do bucket S3 (obrigatório)")

	// Adiciona subcomandos ao grupo s3
	s3Cmd.AddCommand(s3DiscoverCmd)
	s3Cmd.AddCommand(s3ExtractCmd)
	s3Cmd.AddCommand(s3GenerateCmd)
	s3Cmd.AddCommand(s3MigrateCmd)
	s3Cmd.AddCommand(s3PreflightCmd)

	// Adiciona o grupo s3 ao root
	rootCmd.AddCommand(s3Cmd)
}

// boolStr converte bool para string para exibição
func boolStr(b bool) string {
	if b {
		return "sim"
	}
	return "não"
}

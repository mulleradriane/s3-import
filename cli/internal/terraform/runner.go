package terraform

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Runner executa comandos Terraform em um diretório específico.
type Runner struct {
	// WorkDir é o diretório onde os comandos Terraform são executados.
	WorkDir string
	// Stdout é onde a saída padrão do Terraform é redirecionada (padrão: os.Stdout).
	Stdout io.Writer
	// Stderr é onde a saída de erro do Terraform é redirecionada (padrão: os.Stderr).
	Stderr io.Writer
}

// NewRunner cria um novo Runner para o diretório especificado.
func NewRunner(workDir string) *Runner {
	return &Runner{
		WorkDir: workDir,
		Stdout:  os.Stdout,
		Stderr:  os.Stderr,
	}
}

// Init executa `terraform init` no diretório de trabalho.
func (r *Runner) Init(ctx context.Context) error {
	return r.run(ctx, "init", "-input=false")
}

// InitMigrateState executa `terraform init -migrate-state` para migrar o estado.
func (r *Runner) InitMigrateState(ctx context.Context) error {
	return r.run(ctx, "init", "-migrate-state", "-input=false")
}

// Plan executa `terraform plan` e salva o plano em `tfplan`.
func (r *Runner) Plan(ctx context.Context) error {
	return r.run(ctx, "plan", "-input=false", "-out=tfplan")
}

// PlanDestroy executa `terraform plan -destroy`.
func (r *Runner) PlanDestroy(ctx context.Context) error {
	return r.run(ctx, "plan", "-destroy", "-input=false", "-out=tfplan")
}

// Apply executa `terraform apply` com o plano salvo.
func (r *Runner) Apply(ctx context.Context) error {
	return r.run(ctx, "apply", "-input=false", "tfplan")
}

// Import executa `terraform import <address> <id>`.
// address é o endereço do recurso Terraform (ex: module.s3.aws_s3_bucket.this).
// id é o identificador AWS do recurso (ex: nome do bucket).
func (r *Runner) Import(ctx context.Context, address, id string) error {
	return r.run(ctx, "import", "-input=false", address, id)
}

// State executa `terraform state <subcommand> <args...>`.
func (r *Runner) State(ctx context.Context, subcommand string, args ...string) error {
	allArgs := append([]string{"state", subcommand}, args...)
	return r.run(ctx, allArgs...)
}

// Validate executa `terraform validate`.
func (r *Runner) Validate(ctx context.Context) error {
	return r.run(ctx, "validate")
}

// Output executa `terraform output -json` e retorna o JSON.
func (r *Runner) Output(ctx context.Context) (string, error) {
	return r.runCapture(ctx, "output", "-json")
}

// Version retorna a versão do Terraform instalado.
func (r *Runner) Version(ctx context.Context) (string, error) {
	return r.runCapture(ctx, "version")
}

// CheckInstalled verifica se o Terraform está instalado e acessível.
func CheckInstalled() error {
	_, err := exec.LookPath("terraform")
	if err != nil {
		return fmt.Errorf("terraform não encontrado no PATH: instale em https://developer.hashicorp.com/terraform/downloads")
	}
	return nil
}

// run executa um subcomando Terraform com os args fornecidos.
func (r *Runner) run(ctx context.Context, args ...string) error {
	absWorkDir, err := filepath.Abs(r.WorkDir)
	if err != nil {
		return fmt.Errorf("falha ao resolver caminho %q: %w", r.WorkDir, err)
	}

	// Garante que o diretório existe
	if err := os.MkdirAll(absWorkDir, 0755); err != nil {
		return fmt.Errorf("falha ao criar diretório %q: %w", absWorkDir, err)
	}

	cmd := exec.CommandContext(ctx, "terraform", args...)
	cmd.Dir = absWorkDir
	cmd.Stdout = r.Stdout
	cmd.Stderr = r.Stderr

	// Propaga variáveis de ambiente necessárias
	cmd.Env = inheritEnv()

	cmdStr := fmt.Sprintf("terraform %s", strings.Join(args, " "))
	fmt.Printf("   $ %s\n", cmdStr)

	if err := cmd.Run(); err != nil {
		return fmt.Errorf("comando %q falhou: %w", cmdStr, err)
	}

	return nil
}

// runCapture executa um subcomando Terraform e captura a saída.
func (r *Runner) runCapture(ctx context.Context, args ...string) (string, error) {
	absWorkDir, err := filepath.Abs(r.WorkDir)
	if err != nil {
		return "", fmt.Errorf("falha ao resolver caminho %q: %w", r.WorkDir, err)
	}

	cmd := exec.CommandContext(ctx, "terraform", args...)
	cmd.Dir = absWorkDir
	cmd.Env = inheritEnv()

	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("terraform %s falhou: %w", strings.Join(args, " "), err)
	}

	return strings.TrimSpace(string(out)), nil
}

// inheritEnv retorna as variáveis de ambiente do processo atual,
// garantindo que AWS_*, TF_* e HOME sejam propagadas.
func inheritEnv() []string {
	env := os.Environ()

	// Garante que o PATH esteja presente
	hasPath := false
	for _, e := range env {
		if strings.HasPrefix(e, "PATH=") {
			hasPath = true
			break
		}
	}
	if !hasPath {
		env = append(env, "PATH=/usr/local/bin:/usr/bin:/bin")
	}

	return env
}

package output

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/fatih/color"
	"github.com/olekukonko/tablewriter"
)

// Format representa o formato de saída da CLI.
type Format string

const (
	FormatText  Format = "text"
	FormatJSON  Format = "json"
	FormatTable Format = "table"
)

// Colors para status de tier
var (
	ColorAUTO   = color.New(color.FgGreen, color.Bold)
	ColorREVIEW = color.New(color.FgYellow, color.Bold)
	ColorBLOCK  = color.New(color.FgRed, color.Bold)
	ColorInfo   = color.New(color.FgCyan)
	ColorWarn   = color.New(color.FgYellow)
	ColorError  = color.New(color.FgRed)
	ColorOK     = color.New(color.FgGreen)
	ColorBold   = color.New(color.Bold)
)

// Printer gerencia a saída formatada da CLI.
type Printer struct {
	format Format
	out    io.Writer
	err    io.Writer
}

// New cria um novo Printer com o formato e writer especificados.
func New(format Format) *Printer {
	return &Printer{
		format: format,
		out:    os.Stdout,
		err:    os.Stderr,
	}
}

// NewWithWriter cria um Printer com writers customizados (útil para testes).
func NewWithWriter(format Format, out, errW io.Writer) *Printer {
	return &Printer{
		format: format,
		out:    out,
		err:    errW,
	}
}

// TierColor retorna a função de colorização para um tier.
func TierColor(tier string) *color.Color {
	switch strings.ToUpper(tier) {
	case "AUTO":
		return ColorAUTO
	case "REVIEW":
		return ColorREVIEW
	case "BLOCK":
		return ColorBLOCK
	default:
		return color.New(color.Reset)
	}
}

// TierIcon retorna o ícone para um tier.
func TierIcon(tier string) string {
	switch strings.ToUpper(tier) {
	case "AUTO":
		return "✅"
	case "REVIEW":
		return "⚠️ "
	case "BLOCK":
		return "⛔"
	default:
		return "❓"
	}
}

// DiscoveryRow representa uma linha da tabela de discovery.
type DiscoveryRow struct {
	Name   string
	Tier   string
	Reason string
	Team   string
	Env    string
}

// PrintDiscoveryResults exibe os resultados do discover no formato configurado.
func (p *Printer) PrintDiscoveryResults(rows []DiscoveryRow) {
	switch p.format {
	case FormatJSON:
		p.printJSON(rows)
	case FormatTable:
		p.printDiscoveryTable(rows)
	default:
		p.printDiscoveryText(rows)
	}
}

func (p *Printer) printDiscoveryText(rows []DiscoveryRow) {
	// Conta por tier
	counts := map[string]int{"AUTO": 0, "REVIEW": 0, "BLOCK": 0}
	for _, r := range rows {
		counts[r.Tier]++
	}

	fmt.Fprintf(p.out, "\n%s Discovery Results (%d buckets)\n", ColorBold.Sprint("S3"), len(rows))
	fmt.Fprintf(p.out, "%s\n\n", strings.Repeat("─", 60))

	for _, row := range rows {
		tierStr := TierColor(row.Tier).Sprintf("%-6s", row.Tier)
		icon := TierIcon(row.Tier)
		fmt.Fprintf(p.out, "%s %s  %s\n", icon, tierStr, ColorBold.Sprint(row.Name))
		if row.Team != "" || row.Env != "" {
			fmt.Fprintf(p.out, "       %s\n", ColorInfo.Sprintf("time: %s | env: %s", row.Team, row.Env))
		}
		if row.Reason != "" {
			fmt.Fprintf(p.out, "       %s\n", row.Reason)
		}
		fmt.Fprintln(p.out)
	}

	fmt.Fprintf(p.out, "%s\n", strings.Repeat("─", 60))
	fmt.Fprintf(p.out, "Resumo: %s  %s  %s\n",
		ColorAUTO.Sprintf("AUTO: %d", counts["AUTO"]),
		ColorREVIEW.Sprintf("REVIEW: %d", counts["REVIEW"]),
		ColorBLOCK.Sprintf("BLOCK: %d", counts["BLOCK"]),
	)
}

func (p *Printer) printDiscoveryTable(rows []DiscoveryRow) {
	table := tablewriter.NewWriter(p.out)
	table.SetHeader([]string{"Bucket", "Tier", "Time", "Env", "Motivo"})
	table.SetAutoWrapText(true)
	table.SetAutoFormatHeaders(true)
	table.SetHeaderAlignment(tablewriter.ALIGN_LEFT)
	table.SetAlignment(tablewriter.ALIGN_LEFT)
	table.SetBorder(true)
	table.SetRowLine(false)
	table.SetColumnSeparator("│")
	table.SetCenterSeparator("┼")
	table.SetRowSeparator("─")

	for _, row := range rows {
		reason := row.Reason
		if len(reason) > 60 {
			reason = reason[:57] + "..."
		}
		table.Append([]string{
			row.Name,
			row.Tier,
			row.Team,
			row.Env,
			reason,
		})
	}

	table.Render()

	// Resumo
	counts := map[string]int{"AUTO": 0, "REVIEW": 0, "BLOCK": 0}
	for _, r := range rows {
		counts[r.Tier]++
	}
	fmt.Fprintf(p.out, "\nTotal: %d | AUTO: %d | REVIEW: %d | BLOCK: %d\n",
		len(rows), counts["AUTO"], counts["REVIEW"], counts["BLOCK"])
}

func (p *Printer) printJSON(v interface{}) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Fprintf(p.err, "Erro ao serializar JSON: %v\n", err)
		return
	}
	fmt.Fprintln(p.out, string(data))
}

// PrintSuccess exibe uma mensagem de sucesso.
func (p *Printer) PrintSuccess(msg string) {
	fmt.Fprintf(p.out, "%s %s\n", ColorOK.Sprint("✅"), msg)
}

// PrintError exibe uma mensagem de erro.
func (p *Printer) PrintError(msg string) {
	fmt.Fprintf(p.err, "%s %s\n", ColorError.Sprint("⛔"), msg)
}

// PrintWarning exibe um aviso.
func (p *Printer) PrintWarning(msg string) {
	fmt.Fprintf(p.out, "%s %s\n", ColorWarn.Sprint("⚠️ "), msg)
}

// PrintInfo exibe uma mensagem informativa.
func (p *Printer) PrintInfo(msg string) {
	fmt.Fprintf(p.out, "%s %s\n", ColorInfo.Sprint("ℹ️ "), msg)
}

// PrintSection exibe um cabeçalho de seção.
func (p *Printer) PrintSection(title string) {
	fmt.Fprintf(p.out, "\n%s\n%s\n", ColorBold.Sprint(title), strings.Repeat("─", len(title)))
}

// PrintKeyValue exibe um par chave-valor formatado.
func (p *Printer) PrintKeyValue(key, value string) {
	fmt.Fprintf(p.out, "  %-20s %s\n", ColorBold.Sprint(key+":"), value)
}

// PrintPreflightResults exibe os resultados do preflight check.
func (p *Printer) PrintPreflightResults(bucketName string, issues []PreflightIssue) {
	blockers := 0
	warnings := 0
	for _, i := range issues {
		switch i.Severity {
		case "block":
			blockers++
		case "review":
			warnings++
		}
	}

	fmt.Fprintf(p.out, "\n%s\n", ColorBold.Sprintf("Preflight: %s", bucketName))
	fmt.Fprintf(p.out, "%s\n\n", strings.Repeat("─", 50))

	if len(issues) == 0 {
		p.PrintSuccess("Nenhum problema encontrado — bucket pronto para migração")
		return
	}

	for _, issue := range issues {
		switch issue.Severity {
		case "block":
			fmt.Fprintf(p.out, "⛔ %s  %s\n   %s\n\n",
				ColorBLOCK.Sprintf("[%s]", issue.Code),
				ColorBold.Sprint("BLOQUEADOR"),
				issue.Message)
		case "review":
			fmt.Fprintf(p.out, "⚠️  %s  %s\n   %s\n\n",
				ColorREVIEW.Sprintf("[%s]", issue.Code),
				ColorBold.Sprint("REVISÃO"),
				issue.Message)
		default:
			fmt.Fprintf(p.out, "ℹ️  %s\n   %s\n\n",
				ColorInfo.Sprintf("[%s]", issue.Code),
				issue.Message)
		}
	}

	fmt.Fprintf(p.out, "%s\n", strings.Repeat("─", 50))
	if blockers > 0 {
		fmt.Fprintf(p.out, "%s\n", ColorBLOCK.Sprintf("❌ %d bloqueador(es) — migração não pode prosseguir", blockers))
	} else {
		fmt.Fprintf(p.out, "%s\n", ColorREVIEW.Sprintf("⚠️  %d aviso(s) — revise antes de migrar", warnings))
	}
}

// PreflightIssue é o tipo usado na camada de output para issues de preflight.
type PreflightIssue struct {
	Severity string
	Code     string
	Message  string
}

// Progress exibe uma barra de progresso simples para lotes.
type Progress struct {
	total   int
	current int
	out     io.Writer
}

// NewProgress cria um novo rastreador de progresso.
func NewProgress(total int, out io.Writer) *Progress {
	return &Progress{total: total, out: out}
}

// Inc incrementa o contador e exibe o progresso.
func (p *Progress) Inc(name string) {
	p.current++
	pct := (p.current * 100) / p.total
	bar := strings.Repeat("█", pct/5) + strings.Repeat("░", 20-pct/5)
	fmt.Fprintf(p.out, "\r[%s] %3d%% (%d/%d) %s", bar, pct, p.current, p.total, name)
	if p.current == p.total {
		fmt.Fprintln(p.out)
	}
}

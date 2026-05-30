# Mock do módulo de tags — replica a interface do tags module real (ecsbr.net)
# Sem dependências externas para uso em testes locais.

locals {
  env   = lower(trimspace(tostring(lookup(var.tags, "environment", "dev"))))
  team  = lower(trimspace(tostring(lookup(var.tags, "team", "unknown"))))
  prod  = lower(trimspace(tostring(lookup(var.tags, "product", local.team))))
  app   = lower(trimspace(tostring(lookup(var.tags, "application", local.prod))))
  group = lower(trimspace(tostring(lookup(var.tags, "group", "ecs"))))

  normalized = merge(var.tags, var.custom_tags, {
    Environment    = title(local.env)
    Team           = local.team
    Product        = local.prod
    Application    = local.app
    Group          = local.group
    Asset_Category = tostring(lookup(var.tags, "asset_category", "Productive data"))
    Blueprint      = tostring(lookup(var.tags, "blueprint", "S3"))
    Ticket         = tostring(lookup(var.tags, "ticket", "PREENCHER"))
    Repository     = tostring(lookup(var.tags, "repository", ""))
  })
}

output "tags" { value = local.normalized }
output "name" { value = format("%s-%s", local.app, local.env) }

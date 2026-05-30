### A ACL ESTÁ FIXADA COMO PRIVATE DEVIDO A NÃO UTILIZAÇÃO DA PUBLIC CONFORME DIRETRIZES DA EXPERIAN.

locals {
  # Identidade do ambiente
  environment = lower(trimspace(module.tags.tags["Environment"]))
  team        = lower(trimspace(module.tags.tags["Team"]))
  product     = lower(trimspace(module.tags.tags["Product"]))
  application = lower(trimspace(module.tags.tags["Application"]))
  ticket      = lower(trimspace(module.tags.tags["Ticket"]))
  repository  = lower(trimspace(module.tags.tags["Repository"]))
  group       = lower(trimspace(module.tags.tags["Group"]))
  blueprint   = module.tags.tags["Blueprint"]

  # Nome do recurso
  resources_name = trimspace(coalesce(var.tag_legacy_name, format("%s-%s", local.group, module.tags.name)))

  # Configurações de ciclo de vida
  lifecycle_rules       = var.lifecycle_rules
  is_versioning_enabled = var.versioning_configuration == "Enabled"

  # Logging
  logging_target_bucket = coalesce(var.logging_target_bucket, format("%s-%s-logging-s3", local.group, data.aws_caller_identity.current.account_id))
  logging_target_prefix = format("%s/%s/", data.aws_caller_identity.current.account_id, local.resources_name)

  # Criptografia
  sse_algorithm     = var.sse_algorithm
  kms_master_key_id = var.sse_algorithm == "AES256" ? null : coalesce(var.kms_master_key_id, format("arn:aws:kms:%s:%s:alias/%s-default-%s", data.aws_region.current.id, data.aws_caller_identity.current.account_id, local.product, local.environment))
}

#### Buscar Informacoes Externa ####
data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

#### Criar Bucket ####
resource "aws_s3_bucket" "main" {
  bucket = local.resources_name
  tags   = module.tags.tags
}

### Bucket Policy ####
resource "aws_s3_bucket_policy" "main" {
  count  = length(var.policy_json) > 0 ? 1 : 0
  bucket = local.resources_name
  policy = var.policy_json

  depends_on = [
    aws_s3_bucket.main
  ]
}

#### Criação de estrutura de Notificação
module "notification" {
  source = "./modules/notification"
  bucket = try(aws_s3_bucket.main.id, null)

  sqs_notifications    = var.sqs_notifications
  sns_notifications    = var.sns_notifications
  lambda_notifications = var.lambda_notifications
  eventbridge          = var.eventbridge
  create_sns_policy    = var.create_sns_policy
  create_sqs_policy    = var.create_sqs_policy
}

#### Criar LifeCycle ou utiliza uma Personalizada ####
resource "aws_s3_bucket_lifecycle_configuration" "main" {
  bucket = local.resources_name

  rule {
    id     = "Padrao"
    status = "Enabled"
    filter {
      prefix = ""
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  # Regras dinâmicas conforme ambiente/categoria
  dynamic "rule" {
    for_each = (module.tags.tags["Environment"] == "dev" || module.tags.tags["Environment"] == "hml") && var.expiration && module.tags.tags["Asset_Category"] != "Cache" && length(local.lifecycle_rules) == 0 ? [1] : []
    content {
      id     = "Expira em 6 meses dev/hml"
      status = "Enabled"
      filter {
        prefix = ""
      }
      expiration {
        days = 183
      }
    }
  }

  dynamic "rule" {
    for_each = (module.tags.tags["Asset_Category"] == "Productive data" || module.tags.tags["Asset_Category"] == "Model development" || module.tags.tags["Asset_Category"] == "Metadata") && length(local.lifecycle_rules) == 0 ? [1] : []
    content {
      id     = "90 StandardIA -> 180 Glacier"
      status = "Enabled"
      filter {
        prefix = ""
      }
      transition {
        days          = 90
        storage_class = "STANDARD_IA"
      }
      transition {
        days          = 180
        storage_class = "GLACIER_IR"
      }
    }
  }

  dynamic "rule" {
    for_each = (module.tags.tags["Asset_Category"] == "Development" || module.tags.tags["Asset_Category"] == "Staging" || module.tags.tags["Asset_Category"] == "Sandbox") && length(local.lifecycle_rules) == 0 ? [1] : []
    content {
      id     = "30 StandardIA -> 90 Glacier"
      status = "Enabled"
      filter {
        prefix = ""
      }
      transition {
        days          = 30
        storage_class = "STANDARD_IA"
      }
      # Governança: Development / Staging / Sandbox → 90d Glacier IR (não usar dynamic
      # com rule.value aqui: for_each externo é [1], então rule.value.transition era sempre vazio)
      transition {
        days          = 90
        storage_class = "GLACIER_IR"
      }
    }
  }

  dynamic "rule" {
    for_each = (module.tags.tags["Asset_Category"] == "Logs" || module.tags.tags["Asset_Category"] == "Backup") && length(local.lifecycle_rules) == 0 ? [1] : []
    content {
      id     = "30 Glacier"
      status = "Enabled"
      filter {
        prefix = ""
      }
      transition {
        days          = 30
        storage_class = "GLACIER_IR"
      }
      transition {
        days          = 90
        storage_class = "GLACIER"
      }
    }
  }

  dynamic "rule" {
    for_each = (module.tags.tags["Asset_Category"] == "Cache") && length(local.lifecycle_rules) == 0 ? [1] : []
    content {
      id     = "Expira em 45 dias"
      status = "Enabled"
      filter {
        prefix = ""
      }
      expiration {
        days = 45
      }
    }
  }

  # Versionamento: somente prd + Productive data | Embbeded (igual aws_s3_bucket_versioning)
  dynamic "rule" {
    for_each = var.versioning_configuration == "Enabled" && local.environment == "prd" && (module.tags.tags["Asset_Category"] == "Productive data" || module.tags.tags["Asset_Category"] == "Embbeded") && length(local.lifecycle_rules) == 0 ? [1] : []
    content {
      id     = "Deleta as 10 versoes nao atuais apos 180 dias"
      status = "Enabled"
      filter {
        prefix = ""
      }
      noncurrent_version_expiration {
        newer_noncurrent_versions = 10
        noncurrent_days           = 180
      }
    }
  }

  # Regras customizadas via variável
  dynamic "rule" {
    for_each = { for k, v in local.lifecycle_rules : k => v }
    content {
      id     = rule.value.id ## Obrigatorio ID de Rule Unico por Bucket
      status = try(rule.value.status, "Enabled")

      dynamic "noncurrent_version_expiration" {
        for_each = local.is_versioning_enabled ? try(flatten([rule.value.noncurrent_version_expiration]), [1]) : []
        content {
          newer_noncurrent_versions = try(noncurrent_version_expiration.value.newer_noncurrent_versions, 11)
          noncurrent_days           = try(noncurrent_version_expiration.value.days, noncurrent_version_expiration.value.noncurrent_days, 185)
        }
      }

      dynamic "noncurrent_version_transition" {
        for_each = local.is_versioning_enabled ? try(flatten([rule.value.noncurrent_version_transition]), [1]) : []
        content {
          newer_noncurrent_versions = try(noncurrent_version_transition.value.newer_noncurrent_versions, 10)
          noncurrent_days           = try(noncurrent_version_transition.value.days, noncurrent_version_transition.value.noncurrent_days, 0)
          storage_class             = try(noncurrent_version_transition.value.storage_class, "GLACIER_IR")
        }
      }

      dynamic "expiration" {
        for_each = try(flatten([rule.value.expiration]), [])
        content {
          date                         = try(expiration.value.date, null)
          days                         = try(expiration.value.days, null)
          expired_object_delete_marker = try(expiration.value.expired_object_delete_marker, null)
        }
      }

      dynamic "transition" {
        for_each = try(flatten([rule.value.transition]), [])
        content {
          date          = try(transition.value.date, null)
          days          = try(transition.value.days, null)
          storage_class = try(transition.value.storage_class, "GLACIER_IR")
        }
      }

      dynamic "filter" {
        for_each = try(flatten([rule.value.filter]), [])
        content {
          object_size_greater_than = try(filter.value.object_size_greater_than, null)
          object_size_less_than    = try(filter.value.object_size_less_than, null)
          prefix                   = try(filter.value.prefix, "")
          dynamic "tag" {
            for_each = try(filter.value.tags, filter.value.tag, [])
            content {
              key   = tag.key
              value = tag.value
            }
          }
        }
      }
      # Bloco filter padrão (quando o usuário NÃO passa nada)
      dynamic "filter" {
        for_each = length(try(flatten([rule.value.filter]), [])) == 0 ? [true] : []
        content {
          prefix = ""
        }
      }
    }
  }
  depends_on = [aws_s3_bucket.main]
}

#### Operacao de Versionamento ####
resource "aws_s3_bucket_versioning" "main" {
  count  = var.versioning_configuration == "Enabled" && local.environment == "prd" && (module.tags.tags["Asset_Category"] == "Productive data" || module.tags.tags["Asset_Category"] == "Embbeded") ? 1 : 0
  bucket = local.resources_name
  versioning_configuration {
    status = var.versioning_configuration
  }
  depends_on = [aws_s3_bucket.main]
}

#### Operacao de Owner ####
resource "aws_s3_bucket_ownership_controls" "main" {
  bucket = local.resources_name
  rule {
    object_ownership = var.object_ownership
  }
  depends_on = [aws_s3_bucket.main]

}

#### ACL Control ####
resource "aws_s3_bucket_acl" "main" {
  count      = var.object_ownership != "BucketOwnerEnforced" ? 1 : 0
  bucket     = local.resources_name
  acl        = "private"
  depends_on = [aws_s3_bucket_ownership_controls.main, aws_s3_bucket.main]
}

#### Bloquio Acesso Publico ####
resource "aws_s3_bucket_public_access_block" "main" {
  count                   = var.acl == "private" ? 1 : 0
  bucket                  = local.resources_name
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
  depends_on              = [aws_s3_bucket.main]
}

#### Logs Operacao Bucket ####
resource "aws_s3_bucket_logging" "main" {
  bucket        = local.resources_name
  target_bucket = local.logging_target_bucket
  target_prefix = local.logging_target_prefix
  target_object_key_format {
    partitioned_prefix {
      partition_date_source = "EventTime"
    }
  }
  depends_on = [aws_s3_bucket.main]
}

#### Criptografia Bucket ####
resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = local.resources_name
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = local.kms_master_key_id
      sse_algorithm     = local.sse_algorithm
    }
  }
  depends_on = [aws_s3_bucket.main]
}

#### Operacao CORS ####
resource "aws_s3_bucket_cors_configuration" "main" {
  count  = length(var.cors_rules) > 0 ? 1 : 0
  bucket = local.resources_name
  dynamic "cors_rule" {
    for_each = var.cors_rules
    content {
      allowed_headers = try(cors_rule.value.allowed_headers, null) # Optional
      allowed_methods = cors_rule.value.allowed_methods            #Required
      allowed_origins = cors_rule.value.allowed_origins            #Required
      expose_headers  = try(cors_rule.value.expose_headers, null)  # Optional
      max_age_seconds = try(cors_rule.value.max_age_seconds, null) # Optional
    }
  }
  depends_on = [aws_s3_bucket.main]
}

#### Replication Bucket ####
resource "aws_s3_bucket_replication_configuration" "main" {
  count  = length(keys(var.replication_configuration)) > 0 ? 1 : 0
  bucket = local.resources_name
  role   = var.replication_configuration["role"]

  dynamic "rule" {
    for_each = flatten(try([var.replication_configuration["rule"]], [var.replication_configuration["rules"]], []))

    content {
      id       = try(rule.value.id, null)
      priority = try(rule.value.priority, null)
      status   = try(tobool(rule.value.status) ? "Enabled" : "Disabled", title(lower(rule.value.status)), "Enabled")

      dynamic "delete_marker_replication" {
        for_each = flatten(try([rule.value.delete_marker_replication_status], [rule.value.delete_marker_replication], []))

        content {
          # Valid values: "Enabled" or "Disabled"
          status = try(tobool(delete_marker_replication.value) ? "Enabled" : "Disabled", title(lower(delete_marker_replication.value)))
        }
      }

      # Amazon S3 does not support this argument according to:
      # https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_replication_configuration
      # More infor about what does Amazon S3 replicate?
      # https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-what-is-isnot-replicated.html
      dynamic "existing_object_replication" {
        for_each = flatten(try([rule.value.existing_object_replication_status], [rule.value.existing_object_replication], []))

        content {
          # Valid values: "Enabled" or "Disabled"
          status = try(tobool(existing_object_replication.value) ? "Enabled" : "Disabled", title(lower(existing_object_replication.value)))
        }
      }

      dynamic "destination" {
        for_each = try(flatten([rule.value.destination]), [])

        content {
          bucket        = destination.value.bucket
          storage_class = try(destination.value.storage_class, null)
          account       = try(destination.value.account_id, destination.value.account, null)

          dynamic "access_control_translation" {
            for_each = try(flatten([destination.value.access_control_translation]), [])

            content {
              owner = title(lower(access_control_translation.value.owner))
            }
          }

          dynamic "encryption_configuration" {
            for_each = flatten([try(destination.value.encryption_configuration.replica_kms_key_id, destination.value.replica_kms_key_id, [])])

            content {
              replica_kms_key_id = encryption_configuration.value
            }
          }

          dynamic "replication_time" {
            for_each = try(flatten([destination.value.replication_time]), [])

            content {
              # Valid values: "Enabled" or "Disabled"
              status = try(tobool(replication_time.value.status) ? "Enabled" : "Disabled", title(lower(replication_time.value.status)), "Disabled")

              dynamic "time" {
                for_each = try(flatten([replication_time.value.minutes]), [])

                content {
                  minutes = replication_time.value.minutes
                }
              }
            }

          }

          dynamic "metrics" {
            for_each = try(flatten([destination.value.metrics]), [])

            content {
              # Valid values: "Enabled" or "Disabled"
              status = try(tobool(metrics.value.status) ? "Enabled" : "Disabled", title(lower(metrics.value.status)), "Disabled")

              dynamic "event_threshold" {
                for_each = try(flatten([metrics.value.minutes]), [])

                content {
                  minutes = metrics.value.minutes
                }
              }
            }
          }
        }
      }

      dynamic "source_selection_criteria" {
        for_each = try(flatten([rule.value.source_selection_criteria]), [])

        content {
          dynamic "replica_modifications" {
            for_each = flatten([try(source_selection_criteria.value.replica_modifications.enabled, source_selection_criteria.value.replica_modifications.status, [])])

            content {
              # Valid values: "Enabled" or "Disabled"
              status = try(tobool(replica_modifications.value) ? "Enabled" : "Disabled", title(lower(replica_modifications.value)), "Disabled")
            }
          }

          dynamic "sse_kms_encrypted_objects" {
            for_each = flatten([try(source_selection_criteria.value.sse_kms_encrypted_objects.enabled, source_selection_criteria.value.sse_kms_encrypted_objects.status, [])])

            content {
              # Valid values: "Enabled" or "Disabled"
              status = try(tobool(sse_kms_encrypted_objects.value) ? "Enabled" : "Disabled", title(lower(sse_kms_encrypted_objects.value)), "Disabled")
            }
          }
        }
      }

      # Max 1 block - filter - without any key arguments or tags
      dynamic "filter" {
        for_each = length(try(flatten([rule.value.filter]), [])) == 0 ? [true] : []

        content {
        }
      }

      # Max 1 block - filter - with one key argument or a single tag
      dynamic "filter" {
        for_each = [for v in try(flatten([rule.value.filter]), []) : v if max(length(keys(v)), length(try(rule.value.filter.tags, rule.value.filter.tag, []))) == 1]

        content {
          prefix = try(filter.value.prefix, null)

          dynamic "tag" {
            for_each = try(filter.value.tags, filter.value.tag, [])

            content {
              key   = tag.key
              value = tag.value
            }
          }
        }
      }

      # Max 1 block - filter - with more than one key arguments or multiple tags
      dynamic "filter" {
        for_each = [for v in try(flatten([rule.value.filter]), []) : v if max(length(keys(v)), length(try(rule.value.filter.tags, rule.value.filter.tag, []))) > 1]

        content {
          and {
            prefix = try(filter.value.prefix, null)
            tags   = try(filter.value.tags, filter.value.tag, null)
          }
        }
      }
    }
  }

  # Must have bucket versioning enabled first
  depends_on = [aws_s3_bucket_versioning.main[0]]
}

# S3 Metrics 
resource "aws_s3_bucket_metric" "main" {
  count      = var.enable_bucket_metric ? 1 : 0
  bucket     = local.resources_name
  name       = "Default"
  depends_on = [aws_s3_bucket.main]
}

#### Modulos Externos ####
module "tags" {
  source = "./mock_tags"

  tags              = merge({ blueprint = "S3" }, var.tags)
  custom_tags       = var.custom_tags
  legacy_name       = var.tag_legacy_name
  require_data_tags = true
}
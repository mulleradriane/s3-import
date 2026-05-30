variable "tags" {
  type        = map(any)
  description = "Mapa contendo todas as tags"
}

### OPTIONALS
variable "acl" {
  type        = string
  default     = "private"
  description = "ACL of bucket"
}

variable "logging_target_bucket" {
  type        = string
  default     = null
  description = "Bucket S3 que armazena as logs de acesso ao s3."
}

variable "object_ownership" {
  type        = string
  default     = "BucketOwnerEnforced" #Mantem a ACL do Object Ownership desabilitada
  description = "Object Ownership"
  validation {
    condition     = can(regex("^BucketOwnerPreferred$|^BucketOwnerEnforced$|^ObjectWriter$", var.object_ownership))
    error_message = "O valores válidos para o Object Ownership são: BucketOwnerPreferred | BucketOwnerEnforced | ObjectWriter"
  }
}

variable "versioning_configuration" {
  type        = string
  description = "Versionamento"
  default     = "Disabled"
  validation {
    condition     = can(regex("^Enabled$|^Suspended$|^Disabled$", var.versioning_configuration))
    error_message = "O valores válidos para o Status de versionamento são: Enabled | Disabled | Suspended"
  }
}

variable "kms_master_key_id" {
  type        = string
  default     = null
  description = "The ID of an AWS-managed customer master key (CMK) for Amazon SQS or a custom CMK. For more information, see Key Terms."
}

variable "tag_legacy_name" {
  description = "Nome legado do bucket, se aplicável"
  type        = string
  default     = null
}

variable "custom_tags" {
  type        = map(any)
  default     = {}
  description = ""
}

variable "policy_json" {
  type        = string
  default     = ""
  description = "Policy Bucket"
}

variable "sse_algorithm" {
  type    = string
  default = "aws:kms"
}

variable "lifecycle_rules" {
  type        = any
  default     = []
  description = "Configuração de lifecycle customizada"
}

variable "cors_rules" {
  type        = list(any)
  default     = []
  description = "List of maps with cors rules containing allowed_headers, allowed_methods (Required), allowed_origins (Required), expose_headers or max_age_seconds."
}

variable "replication_configuration" {
  type        = any
  default     = {}
  description = "Replication configuration custom."
}

variable "enable_bucket_metric" {
  description = "If true, Create a metrics EntireBucket for S3."
  type        = bool
  default     = false
}

variable "expiration" {
  description = "If true, enabled expiration of objects in S3."
  type        = bool
  default     = true
}

### Variaveis utilizada em Notifications

variable "create_sns_policy" {
  description = "Whether to create a policy for SNS permissions or not?"
  type        = bool
  default     = true
}

variable "create_sqs_policy" {
  description = "Whether to create a policy for SQS permissions or not?"
  type        = bool
  default     = true
}

variable "eventbridge" {
  description = "Whether to enable Amazon EventBridge notifications"
  type        = bool
  default     = null
}

variable "lambda_notifications" {
  description = "Map of S3 bucket notifications to Lambda function"
  type        = any
  default     = {}
}

variable "sqs_notifications" {
  description = "Map of S3 bucket notifications to SQS queue"
  type        = any
  default     = {}
}

variable "sns_notifications" {
  description = "Map of S3 bucket notifications to SNS topic"
  type        = any
  default     = {}
}
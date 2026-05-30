variable "create" {
  description = "Whether to create this resource or not?"
  type        = bool
  default     = true
}

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

variable "bucket" {
  description = "Name of S3 bucket to use"
  type        = string
  default     = ""
}

variable "bucket_arn" {
  description = "ARN of S3 bucket to use in policies"
  type        = string
  default     = null
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
  description = <<-EOT
    Map of S3 bucket notifications to SQS queue. Each entry supports queue_arn, events,
    filter_prefix, filter_suffix, queue_id (optional), and kms_keys (optional list of KMS key ARNs —
    must be a list, e.g. ["arn:aws:kms:..."], not a single string).
    When kms_keys is non-empty, the generated queue policy includes a KMSAllows statement for
    s3.amazonaws.com (required for SSE-KMS encrypted queues).
  EOT
  type        = any
  default     = {}
}

variable "sns_notifications" {
  description = "Map of S3 bucket notifications to SNS topic"
  type        = any
  default     = {}
}


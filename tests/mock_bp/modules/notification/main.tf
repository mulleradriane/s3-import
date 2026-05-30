locals {
  bucket_arn = try(coalesce(var.bucket_arn, "arn:${data.aws_partition.main.partition}:s3:::${var.bucket}"), null)

  # Convert from "arn:aws:sqs:eu-west-1:835367859851:bold-starling-0" into "https://sqs.eu-west-1.amazonaws.com/835367859851/bold-starling-0" if queue_id was not specified
  # queue_url used in aws_sqs_queue_policy is not the same as arn which is used in all other places
  queue_ids = { for k, v in var.sqs_notifications : k => format("https://%s.%s.amazonaws.com/%s/%s", data.aws_arn.queue[k].service, data.aws_arn.queue[k].region, data.aws_arn.queue[k].account, data.aws_arn.queue[k].resource) if try(v.queue_id, "") == "" }

  create_notification = (length(var.sqs_notifications) > 0 || length(var.sns_notifications) > 0 || length(var.lambda_notifications) > 0) && var.create == true ? 1 : 0


}

data "aws_partition" "main" {}

resource "aws_s3_bucket_notification" "main" {
  count = local.create_notification

  bucket = var.bucket

  eventbridge = var.eventbridge
  dynamic "lambda_function" {
    for_each = var.lambda_notifications

    content {
      id                  = try(lambda_function.value.id, lambda_function.key)
      lambda_function_arn = lambda_function.value.function_arn ### Necessario já Possuir lambda Criado
      events              = lambda_function.value.events
      filter_prefix       = try(lambda_function.value.filter_prefix, null)
      filter_suffix       = try(lambda_function.value.filter_suffix, null)
    }
  }

  dynamic "queue" {
    for_each = var.sqs_notifications

    content {
      id            = try(queue.value.id, queue.key)
      queue_arn     = queue.value.queue_arn ### Necessario já possuir Fila SQS Criada
      events        = queue.value.events
      filter_prefix = try(queue.value.filter_prefix, null)
      filter_suffix = try(queue.value.filter_suffix, null)
    }
  }

  dynamic "topic" {
    for_each = var.sns_notifications

    content {
      id            = try(topic.value.id, topic.key)
      topic_arn     = topic.value.topic_arn ### Necessario já possuir Fila SNS Criada
      events        = topic.value.events
      filter_prefix = try(topic.value.filter_prefix, null)
      filter_suffix = try(topic.value.filter_suffix, null)
    }
  }

  depends_on = [
    aws_lambda_permission.allow,
    aws_sqs_queue_policy.allow,
    aws_sns_topic_policy.allow,
  ]
}

# Lambda
resource "aws_lambda_permission" "allow" {
  for_each = var.lambda_notifications

  statement_id_prefix = "AllowLambdaS3BucketNotification-"
  action              = "lambda:InvokeFunction"
  function_name       = each.value.function_name
  qualifier           = try(each.value.qualifier, null)
  principal           = "s3.amazonaws.com"
  source_arn          = local.bucket_arn
  source_account      = try(each.value.source_account, null)
}

# SQS Queue
data "aws_arn" "queue" {
  for_each = var.sqs_notifications

  arn = each.value.queue_arn
}

data "aws_iam_policy_document" "sqs" {
  for_each = { for k, v in var.sqs_notifications : k => v if var.create_sqs_policy }

  statement {
    sid = "AllowSQSS3BucketNotification"

    effect = "Allow"

    actions = [
      "sqs:SendMessage",
    ]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    resources = [each.value.queue_arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.bucket_arn]
    }
  }

  # SSE-KMS na fila: opt-in via kms_keys (lista de ARNs; null/ausente = sem statement KMS)
  dynamic "statement" {
    for_each = length(try(each.value.kms_keys, null) != null ? each.value.kms_keys : []) > 0 ? [1] : []

    content {
      sid    = "KMSAllows"
      effect = "Allow"

      actions = [
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey",
      ]

      principals {
        type        = "Service"
        identifiers = ["s3.amazonaws.com"]
      }

      resources = try(each.value.kms_keys, null) != null ? each.value.kms_keys : []
    }
  }
}

resource "aws_sqs_queue_policy" "allow" {
  for_each = { for k, v in var.sqs_notifications : k => v if var.create_sqs_policy }

  queue_url = try(each.value.queue_id, local.queue_ids[each.key], null)
  policy    = data.aws_iam_policy_document.sqs[each.key].json
}

# SNS Topic
data "aws_iam_policy_document" "sns" {
  for_each = { for k, v in var.sns_notifications : k => v if var.create_sns_policy }

  statement {
    sid = "AllowSNSS3BucketNotification"

    effect = "Allow"

    actions = [
      "sns:Publish",
    ]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    resources = [each.value.topic_arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.bucket_arn]
    }
  }
}

resource "aws_sns_topic_policy" "allow" {
  for_each = { for k, v in var.sns_notifications : k => v if var.create_sns_policy }

  arn    = each.value.topic_arn
  policy = data.aws_iam_policy_document.sns[each.key].json
}



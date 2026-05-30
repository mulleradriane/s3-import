moved {
  from = module.s3.aws_s3_bucket_public_access_block.this
  to   = aws_s3_bucket_public_access_block.main
}
moved {
  from = module.s3.aws_s3_bucket.main
  to   = aws_s3_bucket.main
}
moved {
  from = module.s3.aws_s3_bucket_policy.main
  to   = aws_s3_bucket_policy.main
}
moved {
  from = module.s3.aws_s3_bucket_notification.bucket_notification
  to   = module.s3.module.notification.aws_s3_bucket_notification.main
}
moved {
  from = module.s3.aws_sqs_queue_policy.sqs-policy
  to   = module.s3.module.notification.aws_sqs_queue_policy.allow
}

moved {
  from = aws_s3_bucket.main[0]
  to   = aws_s3_bucket.main
}
output "arn" {
  value       = try(aws_s3_bucket.main.arn, null)
  description = "Bucket ARN."
}

output "account_id" {
  value       = try(data.aws_caller_identity.current.account_id, null)
  description = "Account ID."
}

output "id" {
  value       = try(aws_s3_bucket.main.id, null)
  description = "Bucket ID."
}

output "bucket_domain_name" {
  value       = try(aws_s3_bucket.main.bucket_domain_name, null)
  description = "Bucket Domain name (global/legacy endpoint: bucket.s3.amazonaws.com)."
}

# ✅ Novo output recomendado
output "bucket_regional_domain_name" {
  value       = try(aws_s3_bucket.main.bucket_regional_domain_name, null)
  description = "Bucket Regional Domain name (recommended for CloudFront S3 REST origin: bucket.s3.<region>.amazonaws.com)."
}

output "hosted_zone_id" {
  value       = try(aws_s3_bucket.main.hosted_zone_id, null)
  description = "Route 53 Hosted Zone ID for this bucket's region."
}

output "region" {
  value       = try(aws_s3_bucket.main.region, null)
  description = "The AWS region this bucket resides in."
}

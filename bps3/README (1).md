# Documentação dos Módulos Terraform - ecs-engineering-terraform-blueprint-aws-s3

## Descrição da blueprint: ecs-engineering-terraform-blueprint-aws-s3
## Requirements

No requirements.

## Providers

| Name | Version |
|------|---------|
| <a name="provider_aws"></a> [aws](#provider\_aws) | n/a |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_notification"></a> [notification](#module\_notification) | ./modules/notification | n/a |
| <a name="module_tags"></a> [tags](#module\_tags) | git::https://gitlab.ecsbr.net/ecs/engineering/ecs-engineering-terraform-blueprint-aws-tags//default | 2 |

## Resources

| Name | Type |
|------|------|
| [aws_s3_bucket.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket) | resource |
| [aws_s3_bucket_acl.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_acl) | resource |
| [aws_s3_bucket_cors_configuration.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_cors_configuration) | resource |
| [aws_s3_bucket_lifecycle_configuration.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_lifecycle_configuration) | resource |
| [aws_s3_bucket_logging.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_logging) | resource |
| [aws_s3_bucket_metric.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_metric) | resource |
| [aws_s3_bucket_ownership_controls.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_ownership_controls) | resource |
| [aws_s3_bucket_policy.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_policy) | resource |
| [aws_s3_bucket_public_access_block.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_public_access_block) | resource |
| [aws_s3_bucket_replication_configuration.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_replication_configuration) | resource |
| [aws_s3_bucket_server_side_encryption_configuration.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_server_side_encryption_configuration) | resource |
| [aws_s3_bucket_versioning.main](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_versioning) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_acl"></a> [acl](#input\_acl) | ACL of bucket | `string` | `"private"` | no |
| <a name="input_cors_rules"></a> [cors\_rules](#input\_cors\_rules) | List of maps with cors rules containing allowed\_headers, allowed\_methods (Required), allowed\_origins (Required), expose\_headers or max\_age\_seconds. | `list(any)` | `[]` | no |
| <a name="input_create_sns_policy"></a> [create\_sns\_policy](#input\_create\_sns\_policy) | Whether to create a policy for SNS permissions or not? | `bool` | `true` | no |
| <a name="input_create_sqs_policy"></a> [create\_sqs\_policy](#input\_create\_sqs\_policy) | Whether to create a policy for SQS permissions or not? | `bool` | `true` | no |
| <a name="input_custom_tags"></a> [custom\_tags](#input\_custom\_tags) | n/a | `map(any)` | `{}` | no |
| <a name="input_enable_bucket_metric"></a> [enable\_bucket\_metric](#input\_enable\_bucket\_metric) | If true, Create a metrics EntireBucket for S3. | `bool` | `false` | no |
| <a name="input_eventbridge"></a> [eventbridge](#input\_eventbridge) | Whether to enable Amazon EventBridge notifications | `bool` | `null` | no |
| <a name="input_expiration"></a> [expiration](#input\_expiration) | If true, enabled expiration of objects in S3. | `bool` | `true` | no |
| <a name="input_kms_master_key_id"></a> [kms\_master\_key\_id](#input\_kms\_master\_key\_id) | The ID of an AWS-managed customer master key (CMK) for Amazon SQS or a custom CMK. For more information, see Key Terms. | `string` | `null` | no |
| <a name="input_lambda_notifications"></a> [lambda\_notifications](#input\_lambda\_notifications) | Map of S3 bucket notifications to Lambda function | `any` | `{}` | no |
| <a name="input_lifecycle_rules"></a> [lifecycle\_rules](#input\_lifecycle\_rules) | Configuração de lifecycle customizada | `any` | `[]` | no |
| <a name="input_logging_target_bucket"></a> [logging\_target\_bucket](#input\_logging\_target\_bucket) | Bucket S3 que armazena as logs de acesso ao s3. | `string` | `null` | no |
| <a name="input_object_ownership"></a> [object\_ownership](#input\_object\_ownership) | Object Ownership | `string` | `"BucketOwnerEnforced"` | no |
| <a name="input_policy_json"></a> [policy\_json](#input\_policy\_json) | Policy Bucket | `string` | `""` | no |
| <a name="input_replication_configuration"></a> [replication\_configuration](#input\_replication\_configuration) | Replication configuration custom. | `any` | `{}` | no |
| <a name="input_sns_notifications"></a> [sns\_notifications](#input\_sns\_notifications) | Map of S3 bucket notifications to SNS topic | `any` | `{}` | no |
| <a name="input_sqs_notifications"></a> [sqs\_notifications](#input\_sqs\_notifications) | Map of S3 bucket notifications to SQS queue | `any` | `{}` | no |
| <a name="input_sse_algorithm"></a> [sse\_algorithm](#input\_sse\_algorithm) | n/a | `string` | `"aws:kms"` | no |
| <a name="input_tag_legacy_name"></a> [tag\_legacy\_name](#input\_tag\_legacy\_name) | Nome legado do bucket, se aplicável | `string` | `null` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Mapa contendo todas as tags | `map(any)` | n/a | yes |
| <a name="input_versioning_configuration"></a> [versioning\_configuration](#input\_versioning\_configuration) | Versionamento | `string` | `"Disabled"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_account_id"></a> [account\_id](#output\_account\_id) | Account ID. |
| <a name="output_arn"></a> [arn](#output\_arn) | Bucket ARN. |
| <a name="output_bucket_domain_name"></a> [bucket\_domain\_name](#output\_bucket\_domain\_name) | Bucket Domain name (global/legacy endpoint: bucket.s3.amazonaws.com). |
| <a name="output_bucket_regional_domain_name"></a> [bucket\_regional\_domain\_name](#output\_bucket\_regional\_domain\_name) | Bucket Regional Domain name (recommended for CloudFront S3 REST origin: bucket.s3.<region>.amazonaws.com). |
| <a name="output_hosted_zone_id"></a> [hosted\_zone\_id](#output\_hosted\_zone\_id) | Route 53 Hosted Zone ID for this bucket's region. |
| <a name="output_id"></a> [id](#output\_id) | Bucket ID. |
| <a name="output_region"></a> [region](#output\_region) | The AWS region this bucket resides in. |


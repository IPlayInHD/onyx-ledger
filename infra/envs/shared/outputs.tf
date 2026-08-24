output "ecr_repository_url" { value = aws_ecr_repository.backend.repository_url }
output "ecr_repository_arn" { value = aws_ecr_repository.backend.arn }
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.github.arn }
output "evidence_bucket" { value = aws_s3_bucket.evidence.id }

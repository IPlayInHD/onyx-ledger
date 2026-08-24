output "jwt_secret_arn" { value = aws_secretsmanager_secret.jwt.arn }
output "admission_secret_arn" { value = aws_secretsmanager_secret.admission.arn }
output "redis_url_arn" { value = aws_secretsmanager_secret.redis.arn }
output "db_secret_arns" { value = { for k, s in aws_secretsmanager_secret.db : k => s.arn } }
output "db_password_arns" { value = { for k, s in aws_secretsmanager_secret.db_password : k => s.arn } }
output "kms_key_arn" { value = aws_kms_key.this.arn }

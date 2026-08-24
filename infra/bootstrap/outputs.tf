output "state_bucket" { value = aws_s3_bucket.state.id }
output "lock_table" { value = aws_dynamodb_table.locks.name }
output "state_kms_key_arn" { value = aws_kms_key.state.arn }

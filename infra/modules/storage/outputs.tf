output "documents_bucket" { value = aws_s3_bucket.documents.id }
output "documents_bucket_arn" { value = aws_s3_bucket.documents.arn }
output "legislation_bucket" { value = aws_s3_bucket.legislation.id }
output "legislation_bucket_arn" { value = aws_s3_bucket.legislation.arn }
output "kms_key_arn" { value = aws_kms_key.this.arn }

output "distribution_domain_name" { value = aws_cloudfront_distribution.this.domain_name }
output "distribution_id" { value = aws_cloudfront_distribution.this.id }
output "distribution_arn" { value = aws_cloudfront_distribution.this.arn }
output "frontend_bucket" { value = aws_s3_bucket.frontend.id }
output "frontend_bucket_arn" { value = aws_s3_bucket.frontend.arn }

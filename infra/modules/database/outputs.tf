output "endpoint" { value = aws_db_instance.this.address }
output "port" { value = aws_db_instance.this.port }
output "identifier" { value = aws_db_instance.this.identifier }
output "kms_key_arn" { value = aws_kms_key.this.arn }
output "master_username" { value = aws_db_instance.this.username }
output "master_password" {
  value     = random_password.master.result
  sensitive = true
}

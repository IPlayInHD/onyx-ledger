output "vpc_id" { value = aws_vpc.this.id }
output "public_subnet_ids" { value = [for s in aws_subnet.public : s.id] }
output "private_subnet_ids" { value = [for s in aws_subnet.private : s.id] }
output "data_subnet_ids" { value = [for s in aws_subnet.data : s.id] }
output "alb_security_group_id" { value = aws_security_group.alb.id }
output "tasks_security_group_id" { value = aws_security_group.tasks.id }
output "database_security_group_id" { value = aws_security_group.database.id }
output "cache_security_group_id" { value = aws_security_group.cache.id }

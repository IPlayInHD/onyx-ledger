# =============================================================================
# NETWORK — the VPC everything else sits inside
# =============================================================================
# Three tiers, because two is not enough to say what we mean:
#
#   public    only the load balancer and the NAT gateways
#   private   ECS tasks. Outbound through NAT, nothing inbound from the internet
#   data      RDS and ElastiCache. No route to a NAT gateway at all
#
# The data tier having NO default route is the control that makes
# PUBLIC_DB_EXPOSURE = NO a property of the routing table rather than a promise
# about a security group. A misconfigured group is one mistake away from
# exposure; a subnet with nowhere to send a packet is not.

locals {
  # Two AZs. Three would survive more, and costs another NAT gateway per AZ for
  # a product that has not launched. Multi-AZ RDS already spans two.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  tags = merge(var.tags, { Module = "network" })
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${var.name}-vpc" })
}

# ------------------------------------------------------------------ subnets --
resource "aws_subnet" "public" {
  for_each = { for i, az in local.azs : az => i }

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, each.value)
  # No public IP by default. The ALB gets its addresses from the ELB service;
  # nothing else belongs here, and a task that lands here by accident should not
  # become internet-reachable as a side effect.
  map_public_ip_on_launch = false
  tags                    = merge(local.tags, { Name = "${var.name}-public-${each.key}", Tier = "public" })
}

resource "aws_subnet" "private" {
  for_each = { for i, az in local.azs : az => i }

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, each.value + 4)
  tags              = merge(local.tags, { Name = "${var.name}-private-${each.key}", Tier = "private" })
}

resource "aws_subnet" "data" {
  for_each = { for i, az in local.azs : az => i }

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, each.value + 8)
  tags              = merge(local.tags, { Name = "${var.name}-data-${each.key}", Tier = "data" })
}

# ------------------------------------------------------------------ routing --
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name}-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = merge(local.tags, { Name = "${var.name}-public" })
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# NAT. One in staging, one per AZ in production: a single NAT gateway is a
# single AZ's worth of outbound, and losing it takes every task's egress with
# it. That is survivable for staging and is not for production.
resource "aws_eip" "nat" {
  for_each = var.single_nat_gateway ? toset([local.azs[0]]) : toset(local.azs)
  domain   = "vpc"
  tags     = merge(local.tags, { Name = "${var.name}-nat-${each.key}" })
}

resource "aws_nat_gateway" "this" {
  for_each      = aws_eip.nat
  allocation_id = each.value.id
  subnet_id     = aws_subnet.public[each.key].id
  tags          = merge(local.tags, { Name = "${var.name}-nat-${each.key}" })
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  for_each = aws_subnet.private
  vpc_id   = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.this[local.azs[0]].id : aws_nat_gateway.this[each.key].id
  }
  tags = merge(local.tags, { Name = "${var.name}-private-${each.key}" })
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private[each.key].id
}

# The data tier's table carries no default route. Deliberate — see the header.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.this.id
  tags   = merge(local.tags, { Name = "${var.name}-data" })
}

resource "aws_route_table_association" "data" {
  for_each       = aws_subnet.data
  subnet_id      = each.value.id
  route_table_id = aws_route_table.data.id
}

# ---------------------------------------------------------------- endpoints --
# S3 over a gateway endpoint: free, and it keeps customer document bytes off the
# NAT gateway. Documents are the bulk of this application's egress, so this is
# the one endpoint that pays for itself immediately by not existing on a bill.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat([for rt in aws_route_table.private : rt.id], [aws_route_table.data.id])
  tags              = merge(local.tags, { Name = "${var.name}-s3" })
}

# ---------------------------------------------------------- security groups --
resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Load balancer. Ingress from the internet on 443 only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${var.name}-alb" })
}

# 443 from anywhere is correct for a public load balancer, and it is not the
# control that keeps traffic honest — CloudFront-only admission is enforced by
# the WAF rule in modules/edge, because a security group cannot express "came
# from our distribution".
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet; the WAF admits only CloudFront"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the API tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = var.api_port
  to_port                      = var.api_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "ECS tasks: API and workers."
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${var.name}-tasks" })
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Only the load balancer may reach the API port"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.api_port
  to_port                      = var.api_port
  ip_protocol                  = "tcp"
}

# Egress to anywhere on 443: pulling images, reaching SES, S3 and Secrets
# Manager. Narrowing this to prefix lists is possible and is a later refinement;
# it is listed rather than half-done.
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "Outbound HTTPS to AWS services"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_db" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.database.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_cache" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.cache.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

# The database and cache groups name the task group explicitly. There is no
# CIDR ingress rule on either, so no address range can be widened into one by a
# later edit that looked harmless.
resource "aws_security_group" "database" {
  name        = "${var.name}-database"
  description = "PostgreSQL. Ingress from the task security group only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${var.name}-database" })
}

resource "aws_vpc_security_group_ingress_rule" "db_from_tasks" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from ECS tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "cache" {
  name        = "${var.name}-cache"
  description = "Valkey. Ingress from the task security group only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${var.name}-cache" })
}

resource "aws_vpc_security_group_ingress_rule" "cache_from_tasks" {
  security_group_id            = aws_security_group.cache.id
  description                  = "Valkey from ECS tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

# ---------------------------------------------------------------- flow logs --
# Who talked to what, kept for a fortnight. The first question after a suspected
# compromise is "what did it reach", and the only bad time to start collecting
# the answer is when somebody is asking.
resource "aws_flow_log" "this" {
  vpc_id               = aws_vpc.this.id
  traffic_type         = "REJECT"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow.arn
  iam_role_arn         = aws_iam_role.flow.arn
  tags                 = local.tags
}

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/onyx/${var.name}/vpc-flow"
  retention_in_days = 14
  kms_key_id        = var.logs_kms_key_arn
  tags              = local.tags
}

resource "aws_iam_role" "flow" {
  name = "${var.name}-vpc-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "flow" {
  name = "write-flow-logs"
  role = aws_iam_role.flow.id
  # `<log-group-arn>:*` is the only expressible form: stream names are created
  # at runtime and cannot be enumerated in advance. The grant is still bounded
  # to one log group, which is the narrowing that matters.
  # tfsec:ignore:aws-iam-no-policy-wildcards
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
      ]
      Resource = "${aws_cloudwatch_log_group.flow.arn}:*"
    }]
  })
}

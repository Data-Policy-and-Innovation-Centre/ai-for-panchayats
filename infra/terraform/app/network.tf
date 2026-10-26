# Public-subnet task design, chosen in #59: the chatbot must reach public
# LLM/API endpoints, and a NAT gateway costs more per month than the rest of
# this stack combined. Tasks get public IPs but accept inbound traffic ONLY
# from the load balancer's security group, and there is no database endpoint
# anywhere in this VPC to expose.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.name }
}

# Two AZs because an Application Load Balancer requires at least two subnets.
resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Public entry point"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Behind CloudFront the load balancer is not a public entrance, so it accepts
# nothing from arbitrary addresses -- only CloudFront's published origin-facing
# ranges, and then only with the shared header the listener rule requires.
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  for_each          = var.enable_cdn ? toset([]) : toset(var.ingress_cidrs)
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "HTTP: redirected to HTTPS once a certificate exists"
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  count             = var.enable_cdn ? 1 : 0
  security_group_id = aws_security_group.alb.id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront_origin_facing[0].id
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
  description       = "CloudFront edge locations only"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  # for_each, matching alb_http: keying by index would churn unrelated rules
  # when a CIDR is removed from the middle of the list, and a duplicate entry
  # would fail apply with InvalidPermission.Duplicate.
  for_each          = var.certificate_arn == "" ? toset([]) : toset(var.ingress_cidrs)
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "Application tasks; inbound only from the load balancer"
  vpc_id      = aws_vpc.main.id

  # Outbound is open because the chatbot calls public LLM APIs and downloads
  # the snapshot from S3. There is no database to reach.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# The only way in. Not a CIDR: membership of the ALB security group.
resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
  description                  = "Application port, load balancer only"
}

# Serving the chatbot unauthenticated over plain HTTP is a deliberate,
# time-boxed testing posture. Making it an explicit argument means nobody
# reaches it by accident, and the reason is recorded in the plan.
resource "terraform_data" "public_http_acknowledged" {
  input = var.allow_public_http

  lifecycle {
    precondition {
      condition     = var.certificate_arn != "" || var.enable_cdn || var.allow_public_http
      error_message = "Viewers would get plain HTTP: keep enable_cdn=true, supply a certificate_arn, or set allow_public_http=true to accept it for a bounded test (#59)."
    }

    # ingress_cidrs governs who may reach the LOAD BALANCER. Behind CloudFront
    # nobody reaches it directly, so a narrowed list would be silently
    # inoperative while reading as though access were restricted.
    precondition {
      # toset on both sides: a bare ["0.0.0.0/0"] literal is a tuple, and a
      # tuple never compares equal to the list(string) the variable holds.
      condition     = !var.enable_cdn || toset(var.ingress_cidrs) == toset(["0.0.0.0/0"])
      error_message = "ingress_cidrs cannot restrict access while enable_cdn is true: viewers arrive at CloudFront, not the load balancer, so the list would do nothing. Restrict at CloudFront with a WAF IP set, or set enable_cdn=false to put the load balancer back in front."
    }

    # Two TLS terminators fronting one service is never what someone meant,
    # and the combination silently changes which hostname viewers must use.
    precondition {
      condition     = !(var.enable_cdn && var.certificate_arn != "")
      error_message = "enable_cdn and certificate_arn are mutually exclusive: CloudFront terminates TLS on its own name, so set enable_cdn=false to terminate at the load balancer instead."
    }
  }
}

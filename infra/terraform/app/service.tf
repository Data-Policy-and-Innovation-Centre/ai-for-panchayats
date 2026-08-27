resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # a tag must always mean one image
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  # Only untagged images are expired. ECR cannot express "keep the last N
  # tagged" without a shared tag prefix, and an age rule over tagStatus "any"
  # would eventually delete the image the running service still references --
  # the next task replacement would fail to pull with nothing to roll back to.
  # Tagged images are removed deliberately, not on a timer.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  # Startup downloads and verifies ~1 GB before the port opens, so the health
  # check must not begin counting failures during that window.
  start_period_seconds = 420
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # The snapshot plus its staging copy live here. The default 20 GiB is ample
  # for a ~1 GB artifact; fetch_snapshot refuses to start without headroom.
  ephemeral_storage {
    size_in_gib = 21
  }

  container_definitions = jsonencode([{
    name      = "chatbot"
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = [
      { name = "DB_ENGINE", value = "duckdb_file" },
      { name = "SNAPSHOT_PATH", value = "/var/snapshot/database.duckdb" },
      { name = "AWS_REGION", value = var.region },
      { name = "NGROK_ENABLED", value = "false" },
      { name = "PORT", value = "8000" },
    ]

    # Injected by ECS at start; never present in the task definition, the image
    # or Terraform state.
    secrets = [
      { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "chatbot"
      }
    }
  }])
}

resource "aws_lb" "main" {
  name               = var.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  drop_invalid_header_fields = true

  # Two reasons this is not the 60s default. It must exceed CloudFront's
  # origin_keepalive_timeout, or both ends can close a pooled connection in
  # the same instant and the viewer gets an intermittent 502. And it is a
  # response ceiling in its own right: at 60s it would cut off a slow
  # language-model answer even if CloudFront's own read timeout were raised.
  idle_timeout = 120
}

resource "aws_lb_target_group" "app" {
  name        = var.name
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # Verification takes minutes; the deregistration delay only needs to cover
  # in-flight requests.
  deregistration_delay = 30

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

# Without a certificate this serves plain HTTP, which is acceptable for a
# bounded test and NOT for release -- see #59. Supplying certificate_arn
# switches this to a redirect and adds the HTTPS listener below.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.certificate_arn == "" && !var.enable_cdn ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.app.arn
    }
  }

  # Behind CloudFront the default is a refusal. Anything reaching the load
  # balancer without the shared header did not come through our distribution,
  # so it gets nothing -- see aws_lb_listener_rule.from_cdn below. Target
  # health checks originate inside the load balancer and never pass through
  # listener rules, so this does not affect them.
  dynamic "default_action" {
    for_each = var.enable_cdn ? [1] : []
    content {
      type = "fixed-response"
      fixed_response {
        content_type = "text/plain"
        message_body = "Direct access is not permitted."
        status_code  = "403"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.certificate_arn == "" ? [] : [1]
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener_rule" "from_cdn" {
  count        = var.enable_cdn ? 1 : 0
  listener_arn = aws_lb_listener.http.arn
  priority     = 1

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }

  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_verify[0].result]
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_ecs_service" "app" {
  name            = var.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Long enough for the snapshot download and verification to finish.
  health_check_grace_period_seconds = local.start_period_seconds

  network_configuration {
    subnets = aws_subnet.public[*].id
    # Required to reach S3 and the LLM API without a NAT gateway.
    assign_public_ip = true
    security_groups  = [aws_security_group.task.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "chatbot"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [desired_count]
  }
}

# BOOTSTRAP ORDER on a brand new deployment (empty account/workspace, no
# prior apply): this module creates the ECR repository, the OpenAI secret
# (metadata only, see aws_secretsmanager_secret.openai below), AND a service
# that starts pulling var.image_tag immediately, all in one apply. Neither
# the image nor the secret's value can exist yet, so a first apply run with
# the real desired_count fails every task at launch -- image-pull error, then
# (once an image exists) a secrets-injection error -- and never stabilizes.
# Bootstrap in two passes instead:
#   1. terraform apply -var desired_count=0 -var image_tag=<placeholder>
#   2. docker/build.sh && push the image; aws secretsmanager put-secret-value
#      --secret-id <output.openai_secret_name> --secret-string sk-...
#   3. terraform apply -var desired_count=1 -var image_tag=<real tag>
# Not enforced in code: a precondition here cannot see whether the secret
# has a version or the tag exists in ECR without itself requiring one of
# them to already exist, which is the same chicken-and-egg problem.
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
  family = var.name

  # Keep superseded revisions ACTIVE. Without this the provider deregisters
  # the old revision every time it replaces one, and #57's rollback procedure
  # -- "restore the prior task definition" -- silently has nothing to restore.
  #
  # Found by running the drill rather than by reading the code. Rolling back to
  # :7 one minute after :8 went live returned:
  #
  #   ClientException: TaskDefinition is inactive
  #
  # and `list-task-definitions --status ACTIVE` returned exactly one revision
  # while :1 through :7 were all INACTIVE. An INACTIVE revision cannot start a
  # task or be assigned to a service, so at that moment the only way back from
  # a bad deployment was to rebuild the previous image and re-apply -- minutes
  # of work during an incident, and impossible at all if the previous image tag
  # had been forgotten.
  skip_destroy = true

  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn

  # Graviton is 44% cheaper per vCPU-hour and per GB-hour in ap-south-1
  # ($0.02383 vs $0.04256, $0.00261 vs $0.004655), which is the single largest
  # saving available without giving up capacity. It requires an image built for
  # the matching architecture -- docker/build.sh takes PLATFORM -- and every
  # Python dependency has a cp313 aarch64 manylinux wheel, so nothing compiles
  # from source. A task whose architecture does not match its image fails to
  # start, so these two must change together.
  runtime_platform {
    cpu_architecture        = var.cpu_architecture
    operating_system_family = "LINUX"
  }

  # The architecture and the image must agree, and nothing else checks that.
  # docker/build.sh takes PLATFORM but does not put it in the default tag, so
  # the two are set by hand and can silently disagree. A mismatch is not loud:
  # with minimum_healthy_percent at 100 the old task keeps serving, the new one
  # can never start, `no_running_tasks` stays green because a task IS running,
  # and `apply` reports success. Convention: an arm64 image tag ends in
  # "-arm64", so require that here and fail at plan time instead.
  lifecycle {
    precondition {
      condition = (
        var.cpu_architecture == "ARM64"
        ? endswith(var.image_tag, "-arm64")
        : !endswith(var.image_tag, "-arm64")
      )
      error_message = "image_tag must end in -arm64 when cpu_architecture is ARM64, and must not otherwise. Build with PLATFORM=linux/arm64 and TAG=<tag>-arm64."
    }
  }
  task_role_arn = aws_iam_role.task.arn

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

  # Whichever of these three actually associates the target group with a
  # load balancer -- the http listener itself with certificate_arn == "" and
  # enable_cdn == false, the https listener with a certificate, or the
  # separate from_cdn rule behind CloudFront -- must exist first, or the ECS
  # API can be asked to register targets against a group no listener has
  # attached yet. Depending on only aws_lb_listener.http missed the CDN case:
  # with enable_cdn == true (the default) that listener's own default action
  # is a fixed-response 403, and the forward lives on aws_lb_listener_rule.
  # from_cdn instead. Listing all three is harmless for the two whose count
  # is 0 in a given mode -- a resource with no instances is a no-op dependency.
  depends_on = [
    aws_lb_listener.http,
    aws_lb_listener.https,
    aws_lb_listener_rule.from_cdn,
  ]

  # No autoscaling, deliberately (#54).
  #
  # A new task cannot serve until it has downloaded and SHA-256'd roughly a
  # gigabyte and the application has rebuilt its vector retriever: about two
  # minutes measured end to end, inside a 420s health-check grace. Scale-out
  # that slow cannot absorb a traffic spike -- the spike is over before the
  # capacity arrives -- while each additional task costs about $45/month at the
  # current size and holds its own full copy of the snapshot.
  #
  # Revisit when #72's deferred concurrency work runs against a representative
  # query mix, which needs #61 resolved first. Until then a fixed desired_count
  # is both cheaper and more predictable than a policy that reacts too late.
}

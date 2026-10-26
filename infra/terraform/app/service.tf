# BOOTSTRAP ORDER on a brand new deployment (empty account/workspace, no
# prior apply): this module creates the ECR repository, the OpenAI secret
# (metadata only, see aws_secretsmanager_secret.openai below), AND a service
# that starts pulling var.image_tag immediately, all in one apply. Neither
# the image nor the secret's value can exist yet, so a first apply run with
# the real desired_count fails every task at launch -- image-pull error, then
# (once an image exists) a secrets-injection error -- and never stabilizes.
# Bootstrap in two passes instead:
#   1. terraform apply -var desired_count=0 -var image_tag=<placeholder>
#   2. Build and push an image for <placeholder> to the repository this
#      apply just created. Nothing in THIS branch does that yet -- the
#      Docker build and the OPENAI_API_KEY delivery helper referenced by
#      infra/THREAT_MODEL.md #3 both ship in the separate, still-open
#      deploy/snapshot-packaging branch (PR #74) and are not part of this
#      diff. Until that lands, populate the image and the secret by
#      whatever externally reproducible process produced the tag already
#      running in production. Whatever the method, never pass the key on
#      an `aws` command line -- `--secret-string sk-...` lands in this
#      process's argv, readable by any other user on the host via
#      `ps auxww`. Read it into a variable (prompt, or a gitignored file)
#      and hand the CLI a `file://` path instead, e.g.:
#        printf '%s' "$OPENAI_API_KEY" > "$tmp"   # $tmp from mktemp, chmod 700
#        aws secretsmanager put-secret-value --secret-id <name> \
#          --secret-string "file://$tmp"
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

  # The architecture and the image must agree. A mismatch is not loud: with
  # minimum_healthy_percent at 100 the old task keeps serving, the new one can
  # never start, `no_running_tasks` stays green because a task IS running, and
  # `apply` reports success.
  #
  # This is one of three checks, and it is the weakest of them, so do not read
  # it as the guarantee. It tests the tag STRING. docker/build.sh derives that
  # suffix from PLATFORM and refuses to mint a tag that contradicts it, then
  # asserts the built image's actual architecture with `docker image inspect`.
  # Only that last one inspects the artifact; this one catches an image_tag
  # typed by hand at apply time, which is the case the other two cannot see.
  lifecycle {
    precondition {
      condition = (
        var.cpu_architecture == "ARM64"
        ? endswith(var.image_tag, "-arm64")
        : !endswith(var.image_tag, "-arm64")
      )
      error_message = "image_tag must end in -arm64 when cpu_architecture is ARM64, and must not otherwise. docker/build.sh derives the suffix from PLATFORM, so building with PLATFORM=linux/arm64 (its default) produces a correct tag without setting TAG at all."
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

    # No `healthCheck` here, deliberately (#86).
    #
    # Fargate ignores the image's HEALTHCHECK instruction entirely and reads
    # only this field, so the Dockerfile's is inert in production. The obvious
    # response is to restate it here -- and it would be wrong for a
    # load-balanced service.
    #
    # ALB target health already decides whether this task receives traffic and
    # whether the deployment circuit breaker trips. A container healthCheck
    # would add a SECOND, independent way for the same task to be declared
    # dead, with its own startPeriod that must be kept in agreement with
    # health_check_grace_period_seconds by hand. Two timers governing one
    # startup is how a task gets killed at 300s by the timer nobody remembered
    # while the other still had 120s of grace left.
    #
    # Add one only if a failure mode appears that ALB health cannot see -- a
    # process alive and serving 200s while its database handle is gone, say.
    # Until then the single signal is the correct number of signals.

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
  #
  # How the three startup windows relate (#86), because they are set in three
  # different places and only agree by intention:
  #
  #   1. This grace period, 420s, is the only one that matters to ECS. During
  #      it, ALB target health cannot mark the task unhealthy at all. It has to
  #      exceed the real startup cost: ~1 GB downloaded and SHA-256'd, then the
  #      application's vector retriever rebuilt, measured end to end at about
  #      two minutes. 420s is roughly 3.5x that, which is the headroom that
  #      absorbs a slow S3 day rather than restarting into one.
  #
  #   2. The target group's window is `interval 30 x unhealthy_threshold 5` =
  #      150s to declare a target unhealthy, and `30 x healthy_threshold 2` =
  #      60s to declare it healthy.
  #
  #      These clocks run in PARALLEL with the grace period, not after it. The
  #      ALB starts health-checking the moment the task registers as a target,
  #      which is essentially when the grace period starts. The grace period
  #      does not pause or reset that state machine -- it only governs when the
  #      ECS *scheduler* is allowed to act on a status the ALB has already
  #      computed.
  #
  #      So for a task that never opens its port: unhealthy at ~150s, ignored
  #      until grace expires, then pulled essentially at ~420s. Not 420 + 150.
  #      The grace period is therefore doing exactly one job -- protecting a
  #      SLOW but healthy task, which is the ~2 minute snapshot download --
  #      and it is not a detection delay for a broken one.
  #
  #   3. The Dockerfile's HEALTHCHECK is INERT here. Fargate reads the task
  #      definition's `healthCheck`, never the image's, and this task
  #      definition deliberately defines none -- see the note in
  #      container_definitions above. So ALB target health is the only signal
  #      the circuit breaker can act on, and item 2 is the whole story.
  health_check_grace_period_seconds = local.start_period_seconds

  # An apply used to report success as soon as AWS accepted the update --
  # about 30 seconds -- while the previous task kept serving. A rollout that
  # never became healthy was indistinguishable, from Terraform's point of
  # view, from one that succeeded (#86).
  wait_for_steady_state = true

  # Stated rather than inherited from the provider's defaults, because the
  # pair is a deployment strategy and reading it out of documentation for a
  # different version is how it silently changes.
  #
  # 100/200 with desired_count = 1 means: start the new task, keep the old one
  # serving, and only drain it once the new one is healthy. It costs a second
  # Fargate task for the ~2 minutes of overlap (about $0.008 a deploy) and
  # buys zero-downtime rollouts. The alternative, 0/100, stops the old task
  # first and is a guaranteed outage on a single-task service.
  #
  # #93's post-deploy check exists because of this choice: with the old task
  # serving throughout, a browser check against the public URL can pass in
  # full while the deployment under test never rolled at all.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # Fail a bad rollout instead of leaving it retrying forever, and put the
  # previous task definition back.
  #
  # THE TRAP, and it is the reason this block gets a comment rather than a
  # line: when the breaker trips, ECS marks the deployment failed and rolls
  # back, then the service reaches steady state again -- on the OLD task
  # definition. `wait_for_steady_state` above is satisfied by that. So
  # `terraform apply` can exit 0, and Terraform state will record the image
  # tag it just applied, while the service is running the previous one.
  # Terraform is not wrong here so much as blind: it asked for a change, the
  # change was accepted, and the service is stable.
  #
  # This is a known, unfixed provider limitation rather than a guess:
  # hashicorp/terraform-provider-aws#19519 reports exactly it, and the feature
  # request to make the waiter fail on a rolled-back deployment, #20858, was
  # closed as not planned. So it will not quietly fix itself under a provider
  # bump, and the external check is load-bearing rather than belt-and-braces.
  #
  # What detects it is #93's post-deploy verification, which asserts the
  # service's primary deployment carries the task-definition revision THIS run
  # registered -- not merely that a deployment finished. Nothing inside
  # Terraform can catch this, which is why the check lives outside it.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # With wait_for_steady_state, an apply now blocks for as long as the rollout
  # takes. Sized against the windows documented above: the 420s grace period,
  # by the end of which a broken task is already known-unhealthy and is pulled
  # immediately -- NOT 420 + 150, for the reason item 2 gives -- plus whatever
  # retries the breaker allows before it trips, which AWS does not publish as a
  # fixed number and which is therefore headroom rather than a computed sum,
  # plus room for the pull of a ~600 MB image and the ~2 minute snapshot
  # download each attempt repeats in full (a retried task is a fresh task with
  # fresh ephemeral storage, so nothing is cached). The provider's default
  # of 20m is the same number; it is written down
  # so that a rollout hanging for half an hour is a failed apply with a
  # timeout, rather than a job someone cancels -- and a cancelled apply is
  # exactly what leaves the orphaned S3 lockfile #92 has to deal with.
  timeouts {
    update = "20m"
    create = "20m"
  }

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

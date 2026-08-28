# Alarms for #54 and #59. All of them notify one topic; a subscription is only
# created when alarm_email is set, so applying this in an account without a
# chosen recipient still produces alarms visible in the console rather than
# failing or silently doing nothing.

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

# AWS creates this PendingConfirmation and DELETES it if the emailed link is
# not clicked within three days -- after which every alarm publishes to a topic
# with no subscribers while `terraform plan` stays clean. Confirm the
# subscription, then check it is "Confirmed" in the SNS console.
resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# Cost. NOT a CloudWatch billing alarm: AWS/Billing EstimatedCharges is
# published only in the payer account, and this is a linked member account
# (payer 868644649537). Verified -- `aws cloudwatch list-metrics --namespace
# AWS/Billing --region us-east-1` returns nothing here. A metric alarm would
# apply cleanly, sit green forever and never fire, which is worse than having
# no alarm because it looks like cover.
#
# AWS Budgets does evaluate per linked account. It notifies subscribers
# directly, so it needs no SNS topic and no us-east-1 provider.
#
# Account-scoped rather than project-scoped, and it will therefore also count
# unrelated workloads sharing this account: ListCostAllocationTags is denied to
# linked accounts, so the Project tag cannot be used as a cost filter. See #54.
resource "aws_budgets_budget" "account_monthly" {
  count = var.alarm_email == "" ? 0 : 1

  name         = "${var.name}-account-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_cost_alarm_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Actual spend, after the fact. 80% of the default $450 limit is $360,
  # below the account's ~$390/month baseline documented on
  # monthly_cost_alarm_usd -- so at 80% this fired every month from the
  # unrelated baseline alone, before this deployment spent a cent, which is
  # exactly the false-alarm noise the budget's own threshold was chosen to
  # avoid. 90% ($405) sits above that baseline.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 90
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alarm_email]
  }

  # Forecast, so a runaway is caught while the month can still be salvaged
  # rather than once the money is already spent.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alarm_email]
  }
}

# The service is serving nothing. This is the alarm that matters most: a task
# that cannot verify its snapshot exits before opening its port, so a bad
# snapshot or a bad image shows up here rather than as errors.
resource "aws_cloudwatch_metric_alarm" "no_running_tasks" {
  alarm_name          = "${var.name}-no-running-tasks"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.app.name }
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  period              = 60
  # Five minutes, because a deliberate deployment briefly dips as tasks swap
  # and a rollout should not page anyone.
  evaluation_periods = 5
  # Missing data here means no task is reporting at all, which is the condition
  # being alarmed on -- not an absence of information.
  treat_missing_data = "breaching"

  alarm_description = "No task has been running for five minutes."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name  = "${var.name}-unhealthy-targets"
  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  statistic           = "Maximum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 5
  treat_missing_data  = "notBreaching"

  alarm_description = "A target has been failing its health check for five minutes."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "target_5xx" {
  alarm_name  = "${var.name}-target-5xx"
  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_5XX_Count"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_description = "More than five 5xx responses from the application in five minutes."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

# Measured p95 is about 3.6s and is dominated by LLM round trips rather than
# DuckDB (#72). 15s is therefore roughly four times the observed tail: high
# enough not to fire on a slow model response, low enough to catch the 60s
# CloudFront origin timeout building before users hit it.
resource "aws_cloudwatch_metric_alarm" "slow_responses" {
  alarm_name  = "${var.name}-slow-responses"
  namespace   = "AWS/ApplicationELB"
  metric_name = "TargetResponseTime"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }
  extended_statistic  = "p95"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 15
  period              = 300
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"

  alarm_description = "p95 response time above 15s for ten minutes; the CloudFront origin gives up at 60s."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

# Distinct from HTTPCode_Target_5XX_Count: these are generated by the load
# balancer itself when the target never produced a response at all -- a 504 on
# the 120s idle timeout, or a 502 on a connection it could not use. Those
# requests record no TargetResponseTime and no target status code, so without
# this alarm the most visible failure a user can hit is the one nothing watches.
resource "aws_cloudwatch_metric_alarm" "elb_5xx" {
  alarm_name          = "${var.name}-elb-5xx"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  alarm_description = "More than five load-balancer-generated 5xx in five minutes (target unreachable or timed out)."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

# Abuse tripwire for #59. The application is unauthenticated, so the failure
# mode that costs real money is volume, not errors -- a script can issue
# requests as fast as one task will serve them, and every one spends OpenAI
# credit that no AWS budget can see.
#
# Measured at the load balancer rather than at CloudFront, for two reasons.
# The technical one: CloudFront publishes its metrics only to us-east-1, and a
# CloudWatch alarm cannot notify an SNS topic in another region, so watching it
# would mean a second topic and a second email confirmation. The better one:
# the load balancer sees only what CloudFront could not serve from cache, so a
# crawler pulling the JS bundle repeatedly never reaches this metric.
#
# What it counts is every uncached path, not only /query: /assets/* is the sole
# cached behavior, so page loads land here too. Treat it as an upper bound on
# language-model calls rather than a count of them -- something hammering / can
# trip it having spent nothing. That is still the right alarm, because the
# question being asked is "is a script driving this?", and the answer is yes
# either way.
#
# It does not identify the caller (that needs CloudFront access logging, which
# is not enabled) and it throttles nothing.
resource "aws_cloudwatch_metric_alarm" "request_flood" {
  alarm_name        = "${var.name}-request-flood"
  alarm_description = "Origin request volume far above human browsing. These requests carry the shared pilot credential, so look for a script inside the pilot or a leaked password -- and check OpenAI spend."

  namespace   = "AWS/ApplicationELB"
  metric_name = "RequestCount"
  dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }

  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.request_flood_threshold
  period              = 300
  # Two periods, so one burst -- a demo, a page reloaded in anger -- does not
  # page anyone. Sustained volume does.
  evaluation_periods = 2

  # No traffic is a quiet site, not a broken alarm.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# HTTP Basic authentication at the CloudFront edge, chosen on #59.
#
# WHAT THIS IS FOR. The application is public and the data it serves is public;
# the thing worth protecting is the OpenAI budget behind it. A crawler or a
# script that finds the URL -- and the URL is in a public repository's issue
# text, so "nobody knows the address" was never a control -- can otherwise
# spend real money one query at a time.
#
# WHAT THIS IS NOT. One shared credential is not identity. It cannot say which
# tester asked what, cannot be revoked for one person, and anyone who has it
# can pass it on. It is the right size for a bounded pilot with a known group
# and nothing more. Per-person auth means signed URLs or OIDC, both of which
# are recorded as options on #59.
#
# Running at the VIEWER REQUEST stage is what makes this cheap: the function
# executes before the cache lookup and before the origin, so a rejected request
# never reaches the load balancer, never starts a query, and never bills.

resource "random_password" "basic_auth" {
  count = local.basic_auth_enabled ? 1 : 0

  length = 32
  # No punctuation. This is typed by hand into a browser prompt and pasted into
  # chat messages, where a stray quote or backslash is a support ticket.
  special = false
}

locals {
  # Basic auth lives in the CloudFront distribution, so there is nowhere to put
  # it when the distribution does not exist. The precondition below turns that
  # into an explicit error rather than a silently unprotected deployment.
  basic_auth_enabled = var.enable_basic_auth && var.enable_cdn

  basic_auth_header = local.basic_auth_enabled ? "Basic ${base64encode("${var.basic_auth_username}:${random_password.basic_auth[0].result}")}" : ""
}

resource "terraform_data" "basic_auth_reachable" {
  input = var.enable_basic_auth

  lifecycle {
    precondition {
      condition     = !var.enable_basic_auth || var.enable_cdn
      error_message = "enable_basic_auth requires enable_cdn: the check runs as a CloudFront function, and with enable_cdn=false there is no distribution to attach it to. This will fire on the domain cutover, which is deliberate -- moving TLS to the load balancer moves authentication there too, as an OIDC or Cognito listener rule. Setting enable_basic_auth=false is the way past this only once that rule exists, or if an open endpoint is being accepted knowingly."
    }
  }
}

# The credential is compiled into the function body, so anyone who can call
# cloudfront:GetFunction in this account can read it -- a wider audience than
# the Terraform state bucket, since GetFunction is in the AWS-managed
# ReadOnlyAccess policy. That is the accepted shape of edge authentication:
# CloudFront Functions have no secret store to read from at request time.
resource "aws_cloudfront_function" "basic_auth" {
  count = local.basic_auth_enabled ? 1 : 0

  name    = "${var.name}-basic-auth"
  runtime = "cloudfront-js-2.0"
  comment = "Shared-credential gate for the pilot (#59)"
  publish = true

  code = templatefile("${path.module}/basic_auth.js.tftpl", {
    expected = local.basic_auth_header
    realm    = "Odisha PRDW pilot"
  })
}

# CloudFront exists here for a functional reason, not a performance one.
#
# The dashboard calls crypto.randomUUID() while mounting. That API is only
# defined in a secure context, so over plain HTTP on a public hostname the
# call throws, React never mounts, and the page renders blank -- the app is
# unusable, not merely unencrypted. Serving viewers over HTTPS is therefore a
# hard requirement, and CloudFront supplies a trusted certificate on its own
# *.cloudfront.net name, so this needs no registered domain and no DNS.
#
# When a real domain and ACM certificate exist, set certificate_arn and
# enable_cdn = false to terminate TLS at the load balancer instead.

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Forwards everything the viewer sent, Host included. The load balancer has no
# host-based routing rules, so it does not care what Host says -- but the
# application does: Starlette builds absolute URLs (the trailing-slash 307,
# any RedirectResponse) from the Host it was given. Stripping it would make
# those redirects point at the load balancer's own name, which viewers can no
# longer reach now that its security group admits only CloudFront.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

# Managed-CachingDisabled also disables compression negotiation, so everything
# on the default behavior is served uncompressed. That is not fixable here:
# CloudFront rejects a cache policy that enables Gzip or Brotli while caching
# is disabled ("The parameter EnableAcceptEncodingGzip is invalid for policy
# with caching disabled"), and this origin is an API whose /query answers
# depend on session state, so caching them is not an option. The one large
# payload, the ~1 MB JS bundle, is compressed by the /assets/* behavior below.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

# The load balancer keeps a public DNS name, so restricting its security group
# to CloudFront's address ranges still leaves it reachable by ANY CloudFront
# distribution, including another account's. This shared secret closes that:
# the listener forwards only requests carrying it, and only our distribution
# is configured to add it.
resource "random_password" "origin_verify" {
  count   = var.enable_cdn ? 1 : 0
  length  = 40
  special = false
}

data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  count = var.enable_cdn ? 1 : 0
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_cloudfront_distribution" "app" {
  count = var.enable_cdn ? 1 : 0

  enabled         = true
  is_ipv6_enabled = true
  comment         = var.name

  # Includes India, where the users are. PriceClass_100 would serve Odisha
  # traffic from Europe or North America.
  price_class = "PriceClass_200"

  origin {
    domain_name = aws_lb.main.dns_name
    origin_id   = "alb"

    custom_origin_config {
      http_port  = 80
      https_port = 443
      # KNOWN LIMITATION. This hop crosses the public internet in cleartext,
      # including X-Origin-Verify. CloudFront requires a publicly trusted
      # certificate on a custom origin, and a load balancer with no registered
      # domain cannot obtain one -- so origin TLS is unreachable until a real
      # domain exists, at which point enable_cdn=false is the better answer.
      # The header authenticates the caller; it does not keep it confidential.
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]

      # 60s is the default per-origin quota ceiling, and the binding one: the
      # load balancer's idle_timeout is set to 120 so this is the only limit
      # a slow language-model answer can hit. #72 should measure the tail and
      # request a quota increase if it lands near 60s -- raising the quota
      # alone would not have helped while idle_timeout was also 60.
      origin_read_timeout = 60

      # Strictly below the load balancer's idle_timeout, so CloudFront always
      # retires a pooled connection first rather than racing it.
      origin_keepalive_timeout = 60
    }

    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify[0].result
    }
  }

  # Caching is OFF by default because this origin is an API: /query is a POST
  # whose answer depends on session state. Only the fingerprinted static
  # bundle opts back in, below.
  default_cache_behavior {
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]

    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  # Vite fingerprints these filenames by content, so a changed asset is a
  # changed URL and a long cache lifetime can never serve a stale bundle.
  ordered_cache_behavior {
    path_pattern           = "/assets/*"
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id = data.aws_cloudfront_cache_policy.optimized.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # AWS's own certificate for the assigned *.cloudfront.net name. The
    # minimum TLS version is fixed by AWS and cannot be raised while using it.
    cloudfront_default_certificate = true
  }
}

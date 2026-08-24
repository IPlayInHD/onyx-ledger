# =============================================================================
# SECURITY HEADERS — the same policy netlify.toml states, at CloudFront
# =============================================================================
# `netlify.toml` remains the statement of intent; this is its implementation on
# the new edge. `tests/security/test_edge_header_parity.py` reads both and fails
# if they disagree, so moving hosting cannot quietly drop a header.
#
# The CSP is not copied from netlify.toml, because Netlify's is generated at
# build time to name the backend origin its proxy uses. Here the API is
# same-origin, so `connect-src 'self'` is both simpler and stricter.

resource "aws_cloudfront_response_headers_policy" "security" {
  name    = "${var.name}-onyx-security"
  comment = "Onyx ${var.name}: CSP, HSTS, and the rest of netlify.toml's set."

  security_headers_config {
    content_security_policy {
      override = true
      content_security_policy = join("; ", [
        "default-src 'self'",
        # Vite emits hashed assets and no inline script. No 'unsafe-inline',
        # no 'unsafe-eval' — if a future dependency needs either, it needs a
        # conversation, not a widened policy.
        "script-src 'self'",
        # Styles are bundled; 'unsafe-inline' is required only because the
        # theme applies a handful of custom properties at runtime.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "upgrade-insecure-requests",
      ])
    }

    strict_transport_security {
      override                   = true
      access_control_max_age_sec = 63072000 # two years, as netlify.toml
      include_subdomains         = true
      # preload deliberately NOT set: it is very hard to undo and the domain is
      # not settled. Same decision as netlify.toml, same reason.
      preload = false
    }

    content_type_options { override = true } # nosniff

    frame_options {
      override     = true
      frame_option = "DENY"
    }

    referrer_policy {
      override        = true
      referrer_policy = "strict-origin-when-cross-origin"
    }

    xss_protection {
      override   = true
      protection = false # the header is obsolete and its legacy modes are harmful
      mode_block = false
    }
  }

  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      override = true
      value    = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
    }
    items {
      header   = "Cross-Origin-Opener-Policy"
      override = true
      value    = "same-origin"
    }
    items {
      header   = "Cross-Origin-Resource-Policy"
      override = true
      value    = "same-origin"
    }
  }
}

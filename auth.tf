# --- identity: Cognito ------------------------------------------------------------
resource "aws_cognito_user_pool" "customers" {
  name                     = "${var.project}-customers"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 10
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Stops "user exists" vs "wrong password" from leaking which emails have accounts.
  # (set on the client below via prevent_user_existence_errors)
}

resource "random_id" "domain" {
  byte_length = 3
}

resource "aws_cognito_user_pool_domain" "login" {
  domain       = "${var.project}-${random_id.domain.hex}"
  user_pool_id = aws_cognito_user_pool.customers.id
}

# Public client for a browser app: no secret (it couldn't be kept secret),
# authorization-code flow with PKCE only, no implicit flow.
resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${var.project}-web"
  user_pool_id                         = aws_cognito_user_pool.customers.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [aws_apigatewayv2_stage.default.invoke_url]
  logout_urls                          = [aws_apigatewayv2_stage.default.invoke_url]
  prevent_user_existence_errors        = "ENABLED"
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 1
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

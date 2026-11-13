output "bank_url" {
  description = "Open this in a browser"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "login_domain" {
  value = "${aws_cognito_user_pool_domain.login.domain}.auth.${var.region}.amazoncognito.com"
}

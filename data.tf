resource "aws_dynamodb_table" "bank" {
  name         = var.project
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  # A bank table should be hard to delete by accident. Flip to false before
  # `terraform destroy`.
  deletion_protection_enabled = false
}

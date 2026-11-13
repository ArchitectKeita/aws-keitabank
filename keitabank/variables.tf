variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "keitabank"
}

variable "welcome_cents" {
  description = "Demo money in a new customer's first checking account, in cents"
  type        = number
  default     = 100000
}

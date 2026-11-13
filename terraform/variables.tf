variable "cluster_name" { type = string }
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "alert_email" { type = string }
variable "schedule_expression" {
  type    = string
  default = "rate(1 minute)"
}
variable "heartbeat_stale_seconds" {
  type    = number
  default = 120
}
# Only needed if the EKS API endpoint is private-only
variable "subnet_ids" {
  type    = list(string)
  default = []
}
variable "security_group_ids" {
  type    = list(string)
  default = []
}

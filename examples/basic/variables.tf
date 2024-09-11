variable "hcp_tf_org" {
  type        = string
  description = "HCP Terraform Organization name"
}

variable "tf_run_task_logic_iam_roles" {
  type        = list(string)
  description = "values for the IAM roles to be used by the run task logic"
  default     = []
}

variable "region" {
  type        = string
  description = "AWS region to deploy the resources"
  default     = "us-west-2"
}

variable "tf_run_task_image_tag" {
  type        = string
  description = "value for the docker image tag to be used by the run task logic"
  default     = "latest"
}

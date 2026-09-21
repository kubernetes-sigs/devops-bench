variable "infra_provider" {
  type        = string
  description = "The target cloud provider (gcp, kind)"
}

variable "cluster_name" {
  type        = string
  description = "Name of the cluster to provision"
}

variable "location" {
  type        = string
  description = "Region/zone (GCP) or 'local' (KinD)"
  default     = ""
}

variable "node_count" {
  type        = number
  description = "Number of nodes (1 control-plane + worker nodes). This task requires 1 worker."
  default     = 2
}

variable "machine_type" {
  type        = string
  description = "VM instance type"
  default     = ""
}

variable "project_id" {
  type        = string
  description = "GCP Project ID"
  default     = ""
}

variable "kubeconfig_path" {
  type        = string
  description = "Target path to write kubeconfig (KinD-only)"
  default     = "~/.kube/config"
}

variable "wait_timeout" {
  type        = string
  description = "Seconds each bounded poll in setup.sh will wait before declaring SEED FAIL."
  default     = "180"
}

variable "namespace" {
  type    = string
  default = "default"
}

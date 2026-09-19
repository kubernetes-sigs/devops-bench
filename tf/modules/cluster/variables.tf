# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

variable "infra_provider" {
  type        = string
  description = "The target cloud provider (gcp, kind, vcluster)"

  validation {
    condition     = contains(["gcp", "kind", "vcluster"], var.infra_provider)
    error_message = "infra_provider must be one of: 'gcp', 'kind', 'vcluster'."
  }
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
  description = "Number of worker nodes"
  default     = 3
}

variable "machine_type" {
  type        = string
  description = "VM instance type"
  default     = ""
}

variable "gpu_type" {
  type        = string
  description = "Abstract GPU family: 'l4', 'a100', 't4', or ''"
  default     = ""

  validation {
    condition     = contains(["", "l4", "a100", "t4"], var.gpu_type)
    error_message = "gpu_type must be one of: '', 'l4', 'a100', 't4'."
  }
}

variable "gpu_count" {
  type        = number
  description = "Quantity of GPUs to attach per node"
  default     = 1
}

variable "project_id" {
  type        = string
  description = "GCP Project ID (GCP-only)"
  default     = ""
}

variable "kubeconfig_path" {
  type        = string
  description = "Target path to write kubeconfig (KinD-only)"
  default     = "~/.kube/config"
}

variable "kubernetes_version" {
  type        = string
  description = "The Kubernetes version for the cluster"
  default     = null
}

variable "enable_workload_identity" {
  type        = bool
  description = "Enable GKE Workload Identity (GCP-only)"
  default     = false
}

variable "agent_service_account" {
  type        = string
  description = "The service account email of the agent (GCP-only)"
  default     = ""
}

variable "enable_iap_ssh" {
  type        = bool
  description = "Enable IAP SSH firewall rule (GCP-only)"
  default     = false
}

variable "node_image" {
  type        = string
  description = "The kind node image to use (KinD-only; v1.30+, see tf/modules/cluster/kind/variables.tf)"
  default     = "kindest/node:v1.30.0@sha256:047357ac0cfea04663786a612ba1eaba9702bef25227a794b52890dd8bcd692e"
}


variable "host_kubecontext" {
  type        = string
  description = "Host Kubernetes context to use (vcluster-only)"
  default     = null
}

variable "host_kubeconfig_path" {
  type        = string
  description = "Path to host cluster kubeconfig file (vcluster-only)"
  default     = "~/.kube/config"
}

variable "service_type" {
  type        = string
  description = "Exposure mechanism for the vcluster (LoadBalancer, NodePort, etc.)"
  default     = "LoadBalancer"
}

variable "vcluster_service_cidr" {
  type        = string
  description = "Host cluster's Service CIDR, forwarded to the vcluster chart. Empty keeps the chart default (10.96.0.0/12), which only matches hosts using the Kubernetes default range (e.g. kind); GKE hosts allocate from a different range and require this, or synced Services fail IP allocation and the syncer never syncs pods (blocked 'waiting for DNS service IP')."
  default     = ""
}

variable "node_port" {
  type        = number
  description = "Static port override for local KinD testing (vcluster-only)"
  default     = null
}

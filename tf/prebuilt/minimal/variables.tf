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
  description = "For KinD: path to write the kubeconfig. For vcluster: path to write the vcluster's own kubeconfig (must be run-unique). Unused for gcp."
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
  description = "The kind node image to use (KinD-only)"
  default     = "kindest/node:v1.29.2"
}

variable "host_context" {
  type        = string
  description = "Kube context of the host GKE cluster the virtual cluster runs inside (vcluster-only). Defaults to 'gke_<project_id>_<location>_<host_cluster_name>' when empty"
  default     = ""
}

variable "host_cluster_name" {
  type        = string
  description = "Name of the host GKE cluster the virtual cluster runs inside (vcluster-only); used to derive host_context when host_context is not set"
  default     = ""
}

variable "vcluster_chart_version" {
  type        = string
  description = "Version of the loft-sh vcluster Helm chart to install (vcluster-only)"
  default     = "0.36.1"
}

variable "kubeconfig_path_host" {
  type        = string
  description = "Path to the kubeconfig used to reach the host GKE cluster (vcluster-only). The provider blocks in this root are configured against this same path/context"
  default     = "~/.kube/config"
}

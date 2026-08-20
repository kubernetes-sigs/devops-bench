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

variable "cluster_name" {
  type    = string
  default = "devops-bench-vcluster"
}

variable "project_id" {
  type        = string
  description = "GCP project ID of the host cluster (GKE hosts only; used to derive host_context)"
}

variable "location" {
  type        = string
  description = "Region of the host cluster (e.g. us-central1); used to derive host_context on GKE hosts"
  default     = "us-central1"
}

variable "host_cloud" {
  type        = string
  description = "Cloud the host cluster runs on: 'gke', 'eks', or 'aks'. Only 'gke' is currently implemented end-to-end; 'eks'/'aks' require host_context to be set explicitly"
  default     = "gke"

  validation {
    condition     = contains(["gke", "eks", "aks"], var.host_cloud)
    error_message = "host_cloud must be one of: 'gke', 'eks', 'aks'."
  }
}

variable "host_context" {
  type        = string
  description = "Kube context of the host cluster. On GKE, defaults to 'gke_<project_id>_<location>_<host_cluster_name>' when empty; required for eks/aks host_cloud"
  default     = ""
}

variable "host_cluster_name" {
  type        = string
  description = "Name of the host cluster; used to derive host_context on GKE hosts when host_context is not set"
  default     = ""
}

variable "kubeconfig_path_host" {
  type        = string
  description = "Path to the kubeconfig used to reach the host cluster"
  default     = "~/.kube/config"
}

variable "kubeconfig_path" {
  type        = string
  description = "Path to write the vcluster's own kubeconfig"
}

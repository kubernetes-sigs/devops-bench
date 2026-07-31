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
  type        = string
  description = "Name of the virtual cluster"
  default     = "devops-bench-vcluster"
}

variable "namespace" {
  type        = string
  description = "Host namespace where vcluster is deployed"
  default     = "vcluster-devops-bench"
}

variable "location" {
  type        = string
  description = "Cluster location convention ('local' or cloud region)"
  default     = "local"
}

variable "service_type" {
  type        = string
  description = "Kubernetes Service type for vcluster exposure (NodePort or LoadBalancer)"
  default     = "NodePort"
  validation {
    condition     = contains(["NodePort", "LoadBalancer"], var.service_type)
    error_message = "service_type must be NodePort or LoadBalancer."
  }
}

variable "host_kubecontext" {
  type        = string
  description = "Host Kubernetes context to use"
  default     = null
  nullable    = true
}

variable "infra_provider" {
  type        = string
  description = "Infrastructure provider name"
  default     = null
  nullable    = true
}

variable "project_id" {
  type        = string
  description = "GCP Project ID if applicable"
  default     = null
  nullable    = true
}

variable "kubeconfig_path" {
  type        = string
  description = "Path where Python writes virtual kubeconfig"
  default     = null
  nullable    = true
}

variable "host_kubeconfig_path" {
  type        = string
  description = "Path to host cluster kubeconfig file"
  default     = "~/.kube/config"
}

variable "node_port" {
  type        = number
  description = "Static port override for local KinD testing via TF_VAR_node_port"
  default     = null
  nullable    = true
}

variable "chart_repository" {
  type        = string
  description = "Loft Labs Helm chart repository"
  default     = "https://charts.loft.sh"
}

variable "chart_name_or_path" {
  type        = string
  description = "Helm chart name or local tarball path"
  default     = "vcluster"
}

variable "chart_version" {
  type        = string
  description = "Helm chart version for vcluster"
  default     = "0.20.0"
}

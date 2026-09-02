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
  description = "The cloud provider to use (gcp or kind)"
  type        = string
}

variable "project_id" {
  description = "The GCP project ID (empty for kind)"
  type        = string
  default     = ""
}

variable "cluster_name" {
  description = "The name of the cluster (run-token-prefixed under parallel runs)"
  type        = string
}

variable "location" {
  description = "GCP zone/region or 'local'"
  type        = string
  # Empty by default so the cluster router picks the provider-appropriate value
  # (us-central1-a for GKE, "local" for KinD), matching the minimum/gpu-stress-test
  # stacks. A literal "local" here would be forwarded verbatim to the GKE module.
  default = ""
}

variable "node_count" {
  type    = number
  default = 3
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}


variable "node_image" {
  description = "The KinD node image to use"
  type        = string
  default     = null
}

variable "kubeconfig_path" {
  description = "The path to the local kubeconfig file"
  type        = string
  default     = "~/.kube/config"
}

variable "namespace" {
  description = "Namespace the target workload is deployed into. Must match the harness {{NAMESPACE}} placeholder. 'default' always exists; any other value must be pre-created."
  type        = string
  default     = "default"
}

variable "target_deployment_name" {
  description = "Name of the pre-seeded Deployment + Service the agent must optimize. Must match the harness {{TARGET_DEPLOYMENT_NAME}} placeholder."
  type        = string
  default     = "scale-target"
}

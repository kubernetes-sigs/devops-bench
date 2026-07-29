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
  description = "The name of the GKE or KinD cluster"
  type        = string
}

variable "location" {
  description = "GCP zone/region or 'local'"
  type        = string
  default     = "local"
}

variable "node_count" {
  type    = number
  default = 1
}

variable "machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "node_image" {
  type        = string
  description = "Pinned kindest/node image (v1.30.x; compatible with the pinned Kyverno version)."
  default     = "kindest/node:v1.30.0@sha256:047357ac0cfea04663786a612ba1eaba9702bef25227a794b52890dd8bcd692e"
}

variable "kubeconfig_path" {
  type        = string
  description = "Path kind writes the kubeconfig to (read by the agent)."
  default     = "~/.kube/config"
}

variable "repo_path" {
  type        = string
  description = "Local bare git repo (GitOps source of truth). Empty (default) derives a per-run-unique path from cluster_name so concurrent runs on the shared bastion don't collide (see locals)."
  default     = ""
}

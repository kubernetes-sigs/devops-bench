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
  description = "The target cloud provider (gcp, kind)"
}

variable "project_id" {
  type        = string
  description = "GCP Project ID"
  default     = ""
}

variable "cluster_name" {
  type        = string
  description = "Name of the cluster to provision"
}

variable "location" {
  type        = string
  description = "Region/zone (GCP) or 'local' (KinD)"
}

variable "namespace" {
  type    = string
  default = "default"
}

variable "node_count" {
  type        = number
  description = "Number of worker nodes. The kind sub-module derives its worker list from this, so it must be a number on every provider — a null here fails at plan time."
  default     = 1
}

variable "machine_type" {
  type        = string
  description = "VM instance type (GCP only; the kind sub-module ignores it)."
  default     = "e2-standard-4"
}

variable "start_version" {
  type        = string
  description = "GKE Kubernetes version the cluster starts at (the agent upgrades to the next minor)."
  # NOTE: GKE's supported version range drifts over time, so this default WILL go
  # stale and eventually be rejected ("No valid versions with the prefix ..."). Set
  # it to a currently-supported minor that ALSO has a next minor available; check
  # with: gcloud container get-server-config --zone <zone>
  default = "1.33"
}

variable "node_image" {
  type        = string
  description = "Pinned kindest/node image at the START version the agent upgrades from."
  default     = "kindest/node:v1.30.0@sha256:047357ac0cfea04663786a612ba1eaba9702bef25227a794b52890dd8bcd692e"
}

variable "kubeconfig_path" {
  type        = string
  description = "Path kind writes the kubeconfig to (read by the agent)."
  default     = "~/.kube/config"
}

variable "repo_path" {
  type        = string
  description = "Local bare git repo the agent clones the manifests from. Empty (default) derives a per-run-unique path from cluster_name so concurrent runs on the shared bastion don't collide (see locals)."
  default     = ""
}

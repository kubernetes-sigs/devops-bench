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
  description = "The name of the KinD cluster"
}

variable "kubeconfig_path" {
  type        = string
  description = "Path to write the kubeconfig file"
  default     = "~/.kube/config"
}

variable "node_image" {
  type        = string
  description = "The kind node image (v1.30 through v1.35)"
  # 1.30 is the floor: ValidatingAdmissionPolicy is GA from 1.30 and the
  # pod-security backstop applies one. 1.35 is the ceiling: tehcyx/kind 0.11.0
  # embeds kind v0.31.0, whose kubeadm config can't bootstrap 1.36+.
  default = "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
}

variable "project_id" {
  type        = string
  description = "The GCP project ID (or local-kind)"
  default     = "local-kind"
}

variable "location" {
  type        = string
  description = "The cluster location (or local)"
  default     = "local"
}

variable "node_count" {
  type        = number
  description = "Number of nodes (1 control-plane + worker nodes)"
  default     = 3
}

variable "disable_default_cni" {
  description = "Replace kindnet with Calico so NetworkPolicy is enforced."
  type        = bool
  default     = false
}

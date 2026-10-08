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
  description = "Base name for the run. The two kind clusters are '<cluster_name>' (client/primary, what the prompt calls {{CLUSTER_NAME}}) and '<cluster_name>-peer' (backend). The 'cluster_name' output returns the primary so the harness wires KUBECONFIG to it; setup.sh merges the second context in and also writes it a standalone kubeconfig for verification."
  default     = "devops-bench-kind"
}

variable "location" {
  type        = string
  description = "Always 'local' for kind; kept for deployer compatibility."
  default     = "local"
}

variable "kubeconfig_path" {
  type        = string
  description = "Path kind writes the (primary) kubeconfig to; setup.sh merges the second cluster's context into it."
  default     = "~/.kube/config"
}

variable "node_image" {
  type        = string
  description = "Pinned kindest/node image (v1.30.x)."
  default     = "kindest/node:v1.30.0@sha256:047357ac0cfea04663786a612ba1eaba9702bef25227a794b52890dd8bcd692e"
}

variable "istio_version" {
  type        = string
  description = "Pinned Istio version installed on both clusters."
  default     = "1.23.2"
}

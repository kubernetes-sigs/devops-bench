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

variable "project_id" {
  type        = string
  description = "GCP Project ID"
}

variable "cluster_name" {
  type        = string
  description = "GKE Cluster Name"
}

variable "location" {
  type        = string
  description = "GCP location/zone where GKE cluster is provisioned"
  default     = "us-central1-a"
}

variable "node_count" {
  type        = number
  description = "Number of GKE nodes"
  default     = 3
}

variable "machine_type" {
  type        = string
  description = "Machine type for GKE nodes"
  default     = "e2-standard-2"
}

variable "namespace" {
  type        = string
  description = "Kubernetes Namespace to deploy secret rotation test app"
  default     = "secret-rotation"
}

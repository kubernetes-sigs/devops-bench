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
}

variable "node_count" {
  type        = number
  description = "Number of GKE nodes"
}

variable "machine_type" {
  type        = string
  description = "Machine type for GKE nodes"
}

variable "namespace" {
  type        = string
  description = "Kubernetes Namespace to deploy secret rotation test app"

  # This name is embedded in the run's service account ID as
  # "sa-<namespace>-<8 hex>", and GCP caps a service account ID at 30
  # characters. The fixed parts cost 12, leaving 18. Caught here because the
  # apply-time failure is an opaque IAM 400 raised several resources later.
  validation {
    condition     = length(var.namespace) <= 18
    error_message = "namespace must be at most 18 characters: it is embedded in the 'sa-<namespace>-<8 hex>' service account ID, which GCP caps at 30."
  }
}

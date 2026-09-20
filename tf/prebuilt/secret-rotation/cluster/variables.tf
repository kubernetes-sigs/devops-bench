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

  # Embedded in the "sa-<namespace>-<8 hex>" and "rot-<namespace>-<8 hex>"
  # service account IDs, which GCP caps at 30 characters; the apply-time
  # failure is an opaque IAM 400.
  validation {
    condition = (
      length(var.namespace) <= 17 &&
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.namespace))
    )
    error_message = "namespace must be an RFC 1123 label of 1 to 17 characters: it is embedded in the 'rot-<namespace>-<8 hex>' service account ID, which GCP caps at 30."
  }
}

variable "token_creator_member" {
  type        = string
  description = "IAM member (user:... or serviceAccount:...) allowed to mint tokens for the agent's rotator service account; empty derives it from the provisioner's ADC identity when possible"
  default     = ""
}

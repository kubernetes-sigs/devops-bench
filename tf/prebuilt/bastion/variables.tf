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
  description = "GCP Project ID."
}

variable "zone" {
  type        = string
  description = "GCE zone for the bastion VM."
  default     = "us-central1-a"
}

variable "name" {
  type        = string
  description = "Name of the bastion VM."
  default     = "bench-bastion"
}

variable "machine_type" {
  type        = string
  description = "Machine type for the bastion VM."
  default     = "e2-standard-4"
}

variable "sa_account_id" {
  type        = string
  description = "Account id for the bastion service account (see module docs)."
  default     = "openclaw-vm-sa"
}

variable "assign_external_ip" {
  type        = bool
  description = "Attach an ephemeral external IP for egress (SSH stays IAP-only)."
  default     = true
}

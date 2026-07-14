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
  description = "GCP Project ID the bastion and its service account live in."
}

variable "zone" {
  type        = string
  description = "GCE zone for the bastion VM."
  default     = "us-central1-a"
}

variable "name" {
  type        = string
  description = "Name of the bastion VM (also used to name its firewall rule and network tag)."
  default     = "bench-bastion"
}

variable "machine_type" {
  type        = string
  description = "Machine type for the bastion VM. Needs headroom for tofu + node + the harness."
  default     = "e2-standard-4"
}

variable "boot_disk_gb" {
  type        = number
  description = "Boot disk size in GB."
  default     = 50
}

variable "image" {
  type        = string
  description = "Boot image. Ubuntu 24.04 LTS ships Python 3.12, matching pyproject's requires-python."
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}

variable "sa_account_id" {
  type        = string
  description = <<-EOT
    Account id for the bastion's service account (the VM runs as this SA, so the
    harness uses it as ADC). Defaults to "openclaw-vm-sa" because the existing
    secret-rotation tofu stack references that literal email
    (openclaw-vm-sa@<project>.iam.gserviceaccount.com); override it per harness.
  EOT
  default     = "openclaw-vm-sa"
}

variable "sa_roles" {
  type        = list(string)
  description = <<-EOT
    Project roles granted to the bastion SA so the harness can PROVISION infra by
    running tofu as this SA. Defaults to [] (least privilege) — this reusable
    module grants nothing unless the caller opts in. The prebuilt secret-rotation
    consumer passes the roles its stack needs: create GKE/secrets/service-accounts
    (editor) and set project & SA IAM policy bindings (projectIamAdmin +
    serviceAccountAdmin).
  EOT
  default     = []
}

variable "network" {
  type        = string
  description = "VPC network for the bastion."
  default     = "default"
}

variable "subnetwork" {
  type        = string
  description = "Optional subnetwork. Leave empty to use the network's auto subnet in the VM's region."
  default     = ""
}

variable "assign_external_ip" {
  type        = bool
  description = <<-EOT
    Attach an ephemeral external IP for egress (apt, npm, model APIs). SSH ingress
    is restricted to the IAP range regardless. Set false only if the network has
    Cloud NAT for egress.
  EOT
  default     = true
}

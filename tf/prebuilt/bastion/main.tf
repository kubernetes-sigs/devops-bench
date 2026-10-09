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

# Concrete stack that provisions an eval-harness bastion (CPU by default, or
# GPU with SGLang when var.model is set). See docs/how-to/serve-a-local-model.md.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  zone    = var.zone
}

module "bastion" {
  source = "../../modules/bastion"

  project_id         = var.project_id
  zone               = var.zone
  name               = var.name
  machine_type       = var.machine_type
  boot_disk_gb       = var.boot_disk_gb
  boot_disk_type     = var.boot_disk_type
  image              = var.image
  sa_account_id      = var.sa_account_id
  assign_external_ip = var.assign_external_ip
  gpu_type           = var.gpu_type
  gpu_count          = var.gpu_count
  model              = var.model
  served_name        = var.served_name
  tp                 = var.tp
  context_length     = var.context_length
  reasoning_parser   = var.reasoning_parser
  tool_call_parser   = var.tool_call_parser
  sglang_image       = var.sglang_image
  hf_token_secret    = var.hf_token_secret

  sa_roles = [
    "roles/editor",
    "roles/resourcemanager.projectIamAdmin",
    "roles/iam.serviceAccountAdmin",
  ]
}

output "sa_email" {
  description = "Email of the service account the bastion runs as."
  value       = module.bastion.sa_email
}

output "iap_ssh_command" {
  description = "Command to SSH into the bastion over IAP."
  value       = module.bastion.iap_ssh_command
}

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

variable "boot_disk_gb" {
  type        = number
  description = "Boot disk size in GB."
  default     = 50
}

variable "boot_disk_type" {
  type        = string
  description = "Boot disk type (e.g. hyperdisk-balanced for G4). Null uses the machine-type default."
  default     = null
  nullable    = true
}

variable "image" {
  type        = string
  description = "Boot image override. Null selects the NVIDIA 580 accelerator image when GPU/model is set, else Ubuntu 24.04 LTS."
  default     = null
  nullable    = true
}

variable "sa_account_id" {
  type        = string
  description = "Account id for the bastion service account."
  default     = "openclaw-vm-sa"
}

variable "assign_external_ip" {
  type        = bool
  description = "Attach an ephemeral external IP for egress (SSH stays IAP-only)."
  default     = true
}

variable "gpu_type" {
  type        = string
  description = "Guest accelerator type for N1 shapes (e.g. nvidia-tesla-t4). Leave empty for G4/A2/G2 shapes."
  default     = ""
}

variable "gpu_count" {
  type        = number
  description = "Number of guest accelerators when gpu_type is set."
  default     = 1
}

variable "model" {
  type        = string
  description = "Hugging Face model repo ID to serve with SGLang on the bastion. Empty disables SGLang."
  default     = ""
}

variable "served_name" {
  type        = string
  description = "Model ID advertised at /v1/models. Defaults to var.model when empty."
  default     = ""
}

variable "tp" {
  type        = number
  description = "Tensor parallelism degree for SGLang."
  default     = 1
}

variable "context_length" {
  type        = number
  description = "Context length in tokens passed to SGLang (--context-length). Null omits the flag."
  default     = null
  nullable    = true
}

variable "reasoning_parser" {
  type        = string
  description = "SGLang --reasoning-parser (e.g. qwen3). Empty omits the flag."
  default     = ""
}

variable "tool_call_parser" {
  type        = string
  description = "SGLang --tool-call-parser (e.g. qwen3_coder). Empty omits the flag."
  default     = ""
}

variable "sglang_image" {
  type        = string
  description = "Container image for the SGLang server."
  default     = "lmsysorg/sglang:v0.5.20"
}

variable "hf_token_secret" {
  type        = string
  description = "Secret Manager secret ID holding a Hugging Face token for gated models. Empty skips."
  default     = ""
}

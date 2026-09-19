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

variable "cluster_name" {
  type        = string
  description = <<-EOT
    Base name for the stack. The two regional GKE clusters are named
    "e-<cluster_name>" (primary, east) and "w-<cluster_name>" (standby, west) — the
    region marker is a PREFIX, not a suffix, so it stays within the node-SA
    name-truncation window (see locals in main.tf). The global LB / Cloud SQL
    resources derive their names from it too. Supplied by the harness
    (GKE_CLUSTER_NAME); the "cluster_name" output returns the east cluster so the
    harness credentials it.
  EOT
}

# Declared so `tofu apply -var location=...` from the harness's GCP variable resolver
# does not fail; this stack pins its own regions/zones (see below) and ignores it.
variable "location" {
  type        = string
  description = "Unused. Present only to accept the harness's standard -var location."
  default     = "us-central1-a"
}

variable "namespace" {
  type        = string
  description = "Kubernetes namespace the storefront app runs in (both clusters)."
  default     = "storefront"
}

variable "zone_primary" {
  type        = string
  description = "Zone for the primary (east) GKE cluster."
  default     = "us-east1-b"
}

variable "zone_standby" {
  type        = string
  description = "Zone for the standby (west) GKE cluster."
  default     = "us-west1-b"
}

variable "region_primary" {
  type        = string
  description = "Region for the primary static IP and the Cloud SQL primary."
  default     = "us-east1"
}

variable "region_standby" {
  type        = string
  description = "Region for the standby static IP and the Cloud SQL read replica."
  default     = "us-west1"
}

variable "node_count_primary" {
  type        = number
  description = "Initial node count for the primary (east) cluster."
  default     = 1
}

variable "node_count_standby" {
  type        = number
  description = <<-EOT
    Initial node count for the standby (west) cluster. Kept small on purpose so the
    agent has to scale it up to absorb the redirected production load.
  EOT
  default     = 1
}

variable "machine_type" {
  type        = string
  description = "Machine type for the GKE nodes in both clusters."
  default     = "e2-standard-2"
}

variable "db_tier" {
  type        = string
  description = <<-EOT
    Cloud SQL tier for the primary and replica. Must be a dedicated-core tier so the
    cross-region read replica is supported (shared-core db-f1-micro/g1-small are not).
  EOT
  default     = "db-custom-1-3840"
}

variable "repo_path" {
  type        = string
  description = <<-EOT
    Path to the GitOps bare repo seeded with the app's desired state. Empty (the
    default) derives a per-run-unique path from cluster_name so concurrent runs on
    the shared bastion don't rm -rf + reseed each other's repo (see locals).
  EOT
  default     = ""
}

variable "agent_service_account" {
  type        = string
  description = <<-EOT
    Service account email the agent runs as. Granted roles/container.admin (project
    wide, so it reaches both clusters). Defaults to the OpenClaw VM SA.
  EOT
  default     = ""
}

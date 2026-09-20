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

# A sandboxed agent has no ambient cloud identity, so the stack creates a
# run-unique service account scoped to this cluster's upgrade calls and the
# harness impersonates it for a short-lived token. GKE only.

locals {
  gcp        = var.project_id != "" && var.infra_provider == "gcp"
  cluster_rn = "projects/${var.project_id}/locations/${var.location}/clusters/${var.cluster_name}"
}

resource "random_id" "run" {
  byte_length = 3
}

resource "google_service_account" "agent_upgrader" {
  count        = local.gcp ? 1 : 0
  account_id   = "upg-${random_id.run.hex}-${substr(var.cluster_name, 0, 12)}"
  display_name = "Scoped identity for the sandboxed agent's GKE upgrade calls"
  project      = var.project_id
}

resource "google_project_iam_member" "agent_cluster_admin" {
  count   = local.gcp ? 1 : 0
  project = var.project_id
  role    = "roles/container.clusterAdmin"
  member  = "serviceAccount:${google_service_account.agent_upgrader[0].email}"
  # Node-pool resource names start with the cluster's, so one prefix condition
  # covers both upgrade calls.
  condition {
    title       = "this-run-cluster-only"
    description = "Administer only ${var.cluster_name}"
    expression  = "resource.name.startsWith(\"${local.cluster_rn}\")"
  }
}

resource "google_project_iam_member" "agent_cluster_viewer" {
  count   = local.gcp ? 1 : 0
  project = var.project_id
  role    = "roles/container.clusterViewer"
  member  = "serviceAccount:${google_service_account.agent_upgrader[0].email}"
}

# Token minting: var.token_creator_member, else the provisioner's ADC identity.
# A VM credential without the userinfo-email scope yields null, so the binding
# is skipped and the harness's mint fails rather than running unsandboxed.
data "google_client_openid_userinfo" "provisioner" {
  count = local.gcp ? 1 : 0
}

locals {
  provisioner_email = local.gcp ? data.google_client_openid_userinfo.provisioner[0].email : ""
  token_creator_member = (
    var.token_creator_member != "" ? var.token_creator_member
    : local.provisioner_email == null || local.provisioner_email == "" ? ""
    : endswith(local.provisioner_email, "gserviceaccount.com")
    ? "serviceAccount:${local.provisioner_email}"
    : "user:${local.provisioner_email}"
  )
}

resource "google_service_account_iam_member" "agent_upgrader_token_creator" {
  count              = local.gcp && local.token_creator_member != "" ? 1 : 0
  service_account_id = google_service_account.agent_upgrader[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.token_creator_member
}

output "agent_cloud_identity" {
  description = "Run-unique service account the sandboxed agent's GKE calls run as; empty on kind."
  value       = local.gcp ? google_service_account.agent_upgrader[0].email : ""
}

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

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0.0"
    }
  }
}

# Project-global names (service accounts, Secret Manager secrets) carry a
# per-run suffix. The namespace does not: it is cluster-scoped.
resource "random_id" "run" {
  byte_length = 4
}

module "cluster" {
  source                   = "../../../modules/cluster"
  infra_provider           = "gcp"
  project_id               = var.project_id
  cluster_name             = var.cluster_name
  location                 = var.location
  node_count               = var.node_count
  machine_type             = var.machine_type
  enable_workload_identity = true
  # The agent's runner identity already holds the permissions it needs. A
  # per-run stack must not manage a project IAM binding on a shared principal,
  # because one run's destroy would revoke it for a concurrent run.
  agent_service_account = ""
  enable_iap_ssh        = true
}

resource "google_secret_manager_secret" "db_credentials" {
  secret_id = "db-credentials-${var.namespace}-${random_id.run.hex}"
  project   = var.project_id
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_credentials_v1" {
  secret      = google_secret_manager_secret.db_credentials.id
  secret_data = "compromised-password-v1"
}

resource "google_service_account" "secret_rotation_sa" {
  account_id   = "sa-${var.namespace}-${random_id.run.hex}"
  display_name = "GSA for GKE ExternalSecrets Secret Manager access"
  project      = var.project_id
}

resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  secret_id = google_secret_manager_secret.db_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.secret_rotation_sa.email}"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.secret_rotation_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[external-secrets/external-secrets]"
}

# Identity the sandboxed agent's Secret Manager calls run as: a run-unique
# service account holding only the two roles the rotation needs, on this run's
# secret. The harness impersonates it to mint a short-lived token, and the
# tokenCreator grant below is SA-level so it tears down with the run.
resource "google_service_account" "agent_rotator" {
  account_id   = "rot-${var.namespace}-${random_id.run.hex}"
  display_name = "Scoped identity for the sandboxed agent's Secret Manager calls"
  project      = var.project_id
}

resource "google_secret_manager_secret_iam_member" "agent_version_manager" {
  secret_id = google_secret_manager_secret.db_credentials.id
  role      = "roles/secretmanager.secretVersionManager"
  member    = "serviceAccount:${google_service_account.agent_rotator.email}"
}

resource "google_secret_manager_secret_iam_member" "agent_secret_accessor" {
  secret_id = google_secret_manager_secret.db_credentials.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_rotator.email}"
}

# An explicit var.token_creator_member wins; otherwise the provisioner's
# identity comes from the ADC userinfo endpoint. Without the userinfo-email
# scope the email is null, the binding is skipped and the token mint fails loud.
data "google_client_openid_userinfo" "provisioner" {}

locals {
  provisioner_email = data.google_client_openid_userinfo.provisioner.email
  token_creator_member = (
    var.token_creator_member != "" ? var.token_creator_member
    : local.provisioner_email == null || local.provisioner_email == "" ? ""
    : endswith(local.provisioner_email, "gserviceaccount.com")
    ? "serviceAccount:${local.provisioner_email}"
    : "user:${local.provisioner_email}"
  )
}

resource "google_service_account_iam_member" "agent_rotator_token_creator" {
  count              = local.token_creator_member == "" ? 0 : 1
  service_account_id = google_service_account.agent_rotator.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.token_creator_member
}

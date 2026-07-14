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

# account_id is capped at 30 chars, so we can't fit a long cluster name. Truncating
# the name alone is unsafe: names that share a prefix but differ only in a suffix past
# the cutoff (e.g. "<base>-east" vs "<base>-west" when <base> is long) would collapse to
# the same account_id and collide. Append a short hash of the *full* cluster name so the
# id stays unique per cluster regardless of where the readable part is truncated.
resource "google_service_account" "gke_nodes" {
  account_id   = "gke-nodes-${trim(substr(var.cluster_name, 0, 9), "-")}-${substr(md5(var.cluster_name), 0, 6)}"
  display_name = "GKE Node Service Account for ${var.cluster_name}"
}

resource "google_project_iam_member" "gke_nodes_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_metadata_writer" {
  project = var.project_id
  role    = "roles/stackdriver.resourceMetadata.writer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "gke_nodes_artifact_registry_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_project_iam_member" "agent_container_admin" {
  count   = var.agent_service_account != "" ? 1 : 0
  project = var.project_id
  role    = "roles/container.admin"
  member  = "serviceAccount:${var.agent_service_account}"
}

resource "google_compute_firewall" "allow_iap_ssh" {
  count   = var.enable_iap_ssh ? 1 : 0
  name    = "allow-iap-ssh-${var.cluster_name}"
  network = "default"
  project = var.project_id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges           = ["35.235.240.0/20"]
  target_service_accounts = [google_service_account.gke_nodes.email]
}

resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.location

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false
  min_master_version       = var.kubernetes_version

  dynamic "workload_identity_config" {
    for_each = var.enable_workload_identity ? [1] : []
    content {
      workload_pool = "${var.project_id}.svc.id.goog"
    }
  }
}

locals {
  # Map abstract types to GKE native guest accelerator strings
  abstract_gpu_map = {
    "l4"   = "nvidia-l4"
    "a100" = "nvidia-tesla-a100"
    "t4"   = "nvidia-tesla-t4"
  }

  # Map machine family prefix to GKE native guest accelerator strings
  machine_family_gpu_map = {
    "g2" = "nvidia-l4"
    "a2" = "nvidia-tesla-a100"
  }

  is_g2 = startswith(var.machine_type, "g2-")
  is_a2 = startswith(var.machine_type, "a2-")

  # Determine final GPU attachment parameters
  enable_gpu = var.gpu_type != "" || local.is_g2 || local.is_a2

  # Extract machine family (e.g. "g2" from "g2-standard-4")
  machine_family = split("-", var.machine_type)[0]

  # Deduce GPU type from machine family if not explicitly set but GPU is enabled.
  # This will fail at plan time if machine_family is not in machine_family_gpu_map.
  deduced_gpu_type = var.gpu_type == "" && local.enable_gpu ? local.machine_family_gpu_map[local.machine_family] : ""

  gpu_type = var.gpu_type != "" ? lookup(local.abstract_gpu_map, var.gpu_type) : local.deduced_gpu_type
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "primary-node-pool"
  location   = var.location
  cluster    = google_container_cluster.primary.name
  node_count = var.node_count
  version    = var.kubernetes_version

  node_config {
    preemptible     = false
    machine_type    = var.machine_type
    service_account = google_service_account.gke_nodes.email

    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    dynamic "guest_accelerator" {
      for_each = local.enable_gpu ? [1] : []
      content {
        type  = local.gpu_type
        count = var.gpu_count
        gpu_driver_installation_config {
          gpu_driver_version = "DEFAULT"
        }
      }
    }

    dynamic "workload_metadata_config" {
      for_each = var.enable_workload_identity ? [1] : []
      content {
        mode = "GKE_METADATA"
      }
    }
  }
}

output "cluster_name" {
  value = google_container_cluster.primary.name
}

output "cluster_location" {
  value = google_container_cluster.primary.location
}

output "endpoint" {
  value = google_container_cluster.primary.endpoint
}

output "cluster_ca_certificate" {
  value = google_container_cluster.primary.master_auth[0].cluster_ca_certificate
}


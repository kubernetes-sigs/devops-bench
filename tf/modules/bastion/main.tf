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

# Reusable, harness-agnostic GCE bastion for running the eval harness.
#
# The bastion is a plain Compute Engine VM (NOT Cloud Workstations). It runs as a
# dedicated service account and, via its startup script, installs the full
# harness toolchain plus the openclaw `oc` binary, so the whole harness runs on
# the VM and invokes `oc` as a local subprocess. SSH is reached over IAP.
#
# It deliberately mirrors the plain-Compute patterns in tf/modules/cluster/gke (the agent
# service account + the IAP-SSH firewall rule).

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

# Service account the VM runs as. The harness uses this SA as ADC (via the
# metadata server) for both `gcloud`/`kubectl` and Secret Manager.
resource "google_service_account" "bastion" {
  account_id   = var.sa_account_id
  display_name = "Bastion SA for the DevOps Bench eval harness (${var.name})"
  project      = var.project_id
}

# Provisioning rights so the harness can run tofu AS this SA. See var.sa_roles
# for the rationale and the least-privilege/owner trade-off.
resource "google_project_iam_member" "bastion" {
  for_each = toset(var.sa_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.bastion.email}"
}

# Allow SSH only from Google's IAP TCP-forwarding range, scoped to this VM's tag.
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "allow-iap-ssh-${var.name}"
  network = var.network
  project = var.project_id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["35.235.240.0/20"]
  target_tags   = [var.name]
}

resource "google_compute_instance" "bastion" {
  name         = var.name
  project      = var.project_id
  zone         = var.zone
  machine_type = var.machine_type
  tags         = [var.name]

  # Let tofu stop/restart the VM to apply machine-type or metadata changes.
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = var.image
      size  = var.boot_disk_gb
    }
  }

  network_interface {
    network    = var.network
    subnetwork = var.subnetwork != "" ? var.subnetwork : null

    # Ephemeral external IP for egress; omit entirely when relying on Cloud NAT.
    dynamic "access_config" {
      for_each = var.assign_external_ip ? [1] : []
      content {}
    }
  }

  service_account {
    email  = google_service_account.bastion.email
    scopes = ["cloud-platform"]
  }

  metadata_startup_script = file("${path.module}/startup.sh")
}

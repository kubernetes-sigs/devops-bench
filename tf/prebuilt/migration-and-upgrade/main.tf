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
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

locals {
  # Addresses Pradeep's PR #64 review: seed-repo.sh rm -rf's + recreates this bare
  # repo, so a fixed path is "effectively a global resource and will break parallel
  # runs". cluster_name is run-token-prefixed, making this per-run unique on the
  # shared bastion host. The task prompt references the same path via the
  # {{CLUSTER_NAME}} placeholder. An explicit var.repo_path override still wins.
  repo_path = var.repo_path != "" ? var.repo_path : "~/migration-repo-${var.cluster_name}.git"
}

provider "kind" {}

# GKE/KinD "production" cluster at the START version. The agent migrates the deprecated
# manifests, validates them, applies them, then performs the upgrade.
module "cluster" {
  source                = "../../modules/cluster"
  infra_provider        = var.infra_provider
  project_id            = var.project_id
  cluster_name          = var.cluster_name
  location              = var.location
  node_count            = var.node_count
  machine_type          = var.machine_type
  kubernetes_version    = var.start_version
  node_image            = var.node_image
  kubeconfig_path       = var.kubeconfig_path
  agent_service_account = var.project_id != "" ? "openclaw-vm-sa@${var.project_id}.iam.gserviceaccount.com" : ""
  enable_iap_ssh        = true
}

# Seed the manifests git repo the agent clones (shared script + manifests — same
# source of truth used by the kind stack).
resource "null_resource" "seed_repo" {
  depends_on = [module.cluster]

  triggers = {
    cluster = module.cluster.cluster_name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/seed-repo.sh"
    environment = {
      REPO_PATH     = pathexpand(local.repo_path)
      MANIFESTS_DIR = "${path.module}/manifests"
    }
  }
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}

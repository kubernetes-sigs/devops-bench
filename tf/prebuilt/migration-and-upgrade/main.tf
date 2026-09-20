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
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

locals {
  # Per-run path: seed-repo.sh recreates the repo, so concurrent runs must not
  # share it.
  repo_path = var.repo_path != "" ? var.repo_path : "~/migration-repo-${var.cluster_name}.git"
}

provider "kind" {}

module "cluster" {
  source             = "../../modules/cluster"
  infra_provider     = var.infra_provider
  project_id         = var.project_id
  cluster_name       = var.cluster_name
  location           = var.location
  node_count         = var.node_count
  machine_type       = var.machine_type
  kubernetes_version = var.start_version
  node_image         = var.node_image
  kubeconfig_path    = var.kubeconfig_path
  # Not the operator's identity: the module would own a project-level
  # container.admin binding for it and strip it on teardown. The sandboxed
  # agent uses the upgrader identity in identity.tf instead.
  agent_service_account = ""
  enable_iap_ssh        = true
}

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
      # seed-repo.sh reads $HOME under set -u; a local-exec only inherits what
      # the caller had.
      HOME = pathexpand("~")
    }
  }
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}

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

provider "kind" {}

locals {
  # GitOps repo lives under a scratch root setup.sh owns, not an arbitrary
  # path: setup.sh mints "<scratch_root>/<repo_name>" itself and only ever
  # deletes what it minted (mint-don't-guard), so this variable is a leaf
  # name, never a path. cluster_name is run-token-prefixed, making the
  # derived name per-run unique so concurrent runs on the shared bastion
  # don't collide. The task prompt references the same name via the
  # {{CLUSTER_NAME}} placeholder, against setup.sh's fixed default scratch
  # root (see setup.sh for why that root does not read $TMPDIR).
  repo_name = var.repo_name != "" ? var.repo_name : "opa-repo-${var.cluster_name}.git"
}

# GKE/KinD cluster. Kyverno + the workloads are installed by setup.sh.
module "cluster" {
  source          = "../../modules/cluster"
  infra_provider  = var.infra_provider
  project_id      = var.project_id
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  node_image      = var.node_image
  kubeconfig_path = var.kubeconfig_path
}

# Outside-the-cluster setup: install Kyverno, apply audit policies, deploy the
# violating workloads, and seed the GitOps repo. Runs during `tofu apply`,
# before the agent starts.
resource "null_resource" "setup" {
  depends_on = [module.cluster]

  triggers = {
    cluster = module.cluster.cluster_name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"
    environment = {
      INFRA_PROVIDER = var.infra_provider
      PROJECT_ID     = var.project_id
      CLUSTER_NAME   = module.cluster.cluster_name
      LOCATION       = var.location
      KUBECONFIG     = pathexpand(var.kubeconfig_path)
      REPO_NAME      = local.repo_name
      MANIFESTS_DIR  = "${path.module}/manifests"
    }
  }
}


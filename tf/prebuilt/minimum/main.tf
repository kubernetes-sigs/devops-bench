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
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

# Sweep the Artifact Registry repo the deploy-hello-app agent creates
# (hello-app-<cluster>, project-global and NOT in this stack's tofu state) so it
# doesn't leak across runs — the cluster teardown alone never removes it. No-op
# for the other tasks on this stack (the repo won't exist).
#
# The repo's region is NOT pinned (the agent picks it, and a multi-region default
# like `us` is common), so deleting at one derived location would miss a repo
# that landed elsewhere. Instead, discover wherever the run-unique repo lives by
# listing the project's repos and delete it at its own location. The name match
# is an exact suffix on the run-unique cluster, so a sibling run's repo is never
# touched.
#
# GCP-only: the sweep shells out to `gcloud`, and on KinD there is no Artifact
# Registry to sweep. Without the guard a local KinD run still makes a cloud call,
# against whichever project the developer's `gcloud` is pointed at. Same
# count-guard idiom as modules/cluster/main.tf.
resource "null_resource" "ar_cleanup" {
  count = var.infra_provider == "gcp" ? 1 : 0

  triggers = {
    project = var.project_id
    repo    = "hello-app-${var.cluster_name}"
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      gcloud artifacts repositories list --project='${self.triggers.project}' \
        --format='value(name)' 2>/dev/null \
      | while IFS= read -r full; do
          case "$full" in
            */repositories/'${self.triggers.repo}')
              loc=$(printf '%s\n' "$full" | sed -E 's#.*/locations/([^/]+)/repositories/.*#\1#')
              gcloud artifacts repositories delete '${self.triggers.repo}' \
                --location="$loc" --project='${self.triggers.project}' \
                --quiet 2>/dev/null || true
              ;;
          esac
        done
    EOT
  }
}

provider "kind" {}

module "cluster" {
  source          = "../../modules/cluster"
  infra_provider  = var.infra_provider
  cluster_name    = var.cluster_name
  location        = var.location
  node_count      = var.node_count
  machine_type    = var.machine_type
  project_id      = var.project_id
  kubeconfig_path = var.kubeconfig_path
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}

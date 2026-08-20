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

# This stack is provider-agnostic: it dispatches to tf/modules/cluster, which
# selects the gcp, kind, or vcluster sub-module via `infra_provider`. A module
# invoked with `count`, as those are, cannot declare its own `provider`
# blocks, so all concrete provider configuration lives here in the root
# (mirroring tf/prebuilt/vcluster), even though only one provider is actually
# exercised per run.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
  }
}

locals {
  host_context = var.host_context != "" ? var.host_context : "gke_${var.project_id}_${var.location}_${var.host_cluster_name}"
  # Only the vcluster path talks to the host cluster through helm/kubernetes.
  # Leaving these unset for gcp/kind keeps plans from requiring the host
  # context to exist in the local kubeconfig.
  is_vcluster = var.infra_provider == "vcluster"
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" ? var.location : null
}

provider "helm" {
  kubernetes = {
    config_path    = local.is_vcluster ? pathexpand(var.kubeconfig_path_host) : null
    config_context = local.is_vcluster ? local.host_context : null
  }
}

provider "kubernetes" {
  config_path    = local.is_vcluster ? pathexpand(var.kubeconfig_path_host) : null
  config_context = local.is_vcluster ? local.host_context : null
}

provider "kind" {}

module "cluster" {
  source                   = "../../modules/cluster"
  infra_provider           = var.infra_provider
  cluster_name             = var.cluster_name
  location                 = var.location
  node_count               = var.node_count
  machine_type             = var.machine_type
  gpu_type                 = var.gpu_type
  gpu_count                = var.gpu_count
  project_id               = var.project_id
  kubeconfig_path          = var.kubeconfig_path
  kubernetes_version       = var.kubernetes_version
  enable_workload_identity = var.enable_workload_identity
  agent_service_account    = var.agent_service_account
  enable_iap_ssh           = var.enable_iap_ssh
  node_image               = var.node_image
  host_context             = var.host_context
  host_cluster_name        = var.host_cluster_name
  vcluster_chart_version   = var.vcluster_chart_version
  kubeconfig_path_host     = var.kubeconfig_path_host
}

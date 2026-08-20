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

# This dispatch module instantiates only one concrete cluster sub-module and
# declares no concrete-provider requirements or provider blocks of its own:
# each sub-module only declares required_providers (google in ./gke,
# tehcyx/kind in ./kind, helm/kubernetes in ./vcluster) and inherits
# the actual provider configuration implicitly from whatever root caller
# instantiates this module, so a KinD-only run does not pull in the GCP
# provider plugin. A module invoked with `count`, as these are, cannot itself
# declare `provider` blocks, so a caller that dispatches to "vcluster" must
# configure the default helm/kubernetes providers itself, pointed at the host
# cluster (see tf/prebuilt/vcluster for an example).

module "gke" {
  source                   = "./gke"
  count                    = var.infra_provider == "gcp" ? 1 : 0
  project_id               = var.project_id
  location                 = var.location != "" ? var.location : "us-central1-a"
  cluster_name             = var.cluster_name
  node_count               = var.node_count
  machine_type             = var.machine_type != "" ? var.machine_type : "e2-standard-2"
  kubernetes_version       = var.kubernetes_version
  enable_workload_identity = var.enable_workload_identity
  agent_service_account    = var.agent_service_account
  enable_iap_ssh           = var.enable_iap_ssh
  gpu_type                 = var.gpu_type
  gpu_count                = var.gpu_count
}

module "kind" {
  source          = "./kind"
  count           = var.infra_provider == "kind" ? 1 : 0
  cluster_name    = var.cluster_name
  kubeconfig_path = var.kubeconfig_path
  node_image      = var.node_image
  project_id      = var.project_id
  location        = var.location != "" ? var.location : "local"
  node_count      = var.node_count
}

module "vcluster" {
  source                 = "./vcluster"
  count                  = var.infra_provider == "vcluster" ? 1 : 0
  cluster_name           = var.cluster_name
  location               = var.location != "" ? var.location : "us-central1"
  project_id             = var.project_id
  host_cloud             = var.host_cloud
  host_context           = var.host_context
  host_cluster_name      = var.host_cluster_name
  kubeconfig_path_host   = var.kubeconfig_path_host
  kubeconfig_path        = var.kubeconfig_path
  vcluster_chart_version = var.vcluster_chart_version
}


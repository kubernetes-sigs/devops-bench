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
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
}

locals {
  host_context = var.host_context != "" ? var.host_context : (
    var.host_cloud == "gke" ? "gke_${var.project_id}_${var.location}_${var.host_cluster_name}" : ""
  )
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path_host)
    config_context = local.host_context
  }
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path_host)
  config_context = local.host_context
}

module "vcluster" {
  source               = "../../modules/cluster/vcluster"
  cluster_name         = var.cluster_name
  project_id           = var.project_id
  location             = var.location
  host_cloud           = var.host_cloud
  host_context         = var.host_context
  host_cluster_name    = var.host_cluster_name
  kubeconfig_path_host = var.kubeconfig_path_host
  kubeconfig_path      = var.kubeconfig_path
}

output "cluster_name" {
  value = module.vcluster.cluster_name
}

output "cluster_location" {
  value = module.vcluster.cluster_location
}

output "kubeconfig_path" {
  value = module.vcluster.kubeconfig_path
}

output "endpoint" {
  value = module.vcluster.endpoint
}

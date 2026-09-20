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
    kind = {
      source  = "tehcyx/kind"
      version = ">= 0.5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.0.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

provider "kind" {}

# Three control-plane nodes give a real three-member stacked etcd; the worker
# hosts the workloads, since control-plane nodes are tainted.
resource "kind_cluster" "default" {
  name            = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = pathexpand(var.kubeconfig_path)
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }
    node {
      role = "control-plane"
    }
    node {
      role = "control-plane"
    }
    node {
      role = "worker"
    }
  }
}

provider "kubernetes" {
  host                   = kind_cluster.default.endpoint
  client_certificate     = kind_cluster.default.client_certificate
  client_key             = kind_cluster.default.client_key
  cluster_ca_certificate = kind_cluster.default.cluster_ca_certificate
}

provider "helm" {
  kubernetes {
    host                   = kind_cluster.default.endpoint
    client_certificate     = kind_cluster.default.client_certificate
    client_key             = kind_cluster.default.client_key
    cluster_ca_certificate = kind_cluster.default.cluster_ca_certificate
  }
}

resource "helm_release" "workloads" {
  name             = "cp-recovery-workloads"
  chart            = "${path.module}/cp-recovery-chart"
  namespace        = var.namespace
  create_namespace = true

  set {
    name  = "namespace"
    value = var.namespace
  }

  set {
    name  = "clusterName"
    value = var.cluster_name
  }
}

# Runs during tofu apply, before the agent starts, and leaves nothing in the
# cluster that describes the fault.
resource "null_resource" "inject_fault" {
  depends_on = [helm_release.workloads]

  triggers = {
    cluster   = kind_cluster.default.name
    namespace = var.namespace
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/inject-fault.sh"
    environment = {
      CLUSTER_NAME = var.cluster_name
      NAMESPACE    = var.namespace
      KUBECONFIG   = pathexpand(var.kubeconfig_path)
      # inject-fault.sh reads $HOME under set -u; a local-exec only inherits what
      # the caller had.
      HOME = pathexpand("~")
    }
  }
}

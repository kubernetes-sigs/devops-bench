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
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.25"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }
}

# The kubernetes and helm providers map config_path and config_context
# directly to the host Kubernetes cluster inputs, ensuring OpenTofu
# communicates exclusively with the intended host Kubernetes API server
# without falling back to ambient kubeconfig contexts.
provider "kubernetes" {
  config_path    = pathexpand(var.host_kubeconfig_path)
  config_context = var.host_kubecontext
}

provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.host_kubeconfig_path)
    config_context = var.host_kubecontext
  }
}

resource "kubernetes_namespace" "vcluster" {
  metadata {
    name = var.namespace
    labels = {
      app                       = "vcluster"
      "devops-bench/run-scoped" = "true"
    }
  }
}

resource "kubernetes_service" "vcluster_exposure" {
  metadata {
    name      = var.cluster_name
    namespace = kubernetes_namespace.vcluster.metadata[0].name
  }

  spec {
    type = var.service_type
    selector = {
      app     = "vcluster"
      release = var.cluster_name
    }
    port {
      port        = 443
      target_port = 8443
      protocol    = "TCP"
      node_port   = var.node_port
    }
  }

  lifecycle {
    ignore_changes = [spec[0].port[0].node_port]
  }
}

resource "kubernetes_resource_quota" "vcluster_quota" {
  metadata {
    name      = "vcluster-quota"
    namespace = kubernetes_namespace.vcluster.metadata[0].name
  }

  spec {
    hard = {
      "limits.cpu"             = "7"
      "limits.memory"          = "28Gi"
      "requests.cpu"           = "7"
      "requests.memory"        = "28Gi"
      "requests.storage"       = "50Gi"
      "persistentvolumeclaims" = "10"
    }
  }
}

resource "kubernetes_limit_range" "vcluster_limits" {
  metadata {
    name      = "vcluster-limits"
    namespace = kubernetes_namespace.vcluster.metadata[0].name
  }

  spec {
    limit {
      type = "Container"
      default = {
        cpu    = "1"
        memory = "2Gi"
      }
      default_request = {
        cpu    = "200m"
        memory = "512Mi"
      }
      max = {
        cpu    = "6"
        memory = "24Gi"
      }
    }
  }
}

data "kubernetes_nodes" "host_nodes" {}

locals {
  host_node_ip = try(
    coalesce(
      try([for a in data.kubernetes_nodes.host_nodes.nodes[0].status[0].addresses : a.address if a.type == "ExternalIP"][0], null),
      try([for a in data.kubernetes_nodes.host_nodes.nodes[0].status[0].addresses : a.address if a.type == "InternalIP"][0], null),
      "127.0.0.1"
    ),
    "127.0.0.1"
  )
  external_endpoint = var.service_type == "NodePort" ? "${local.host_node_ip}:${kubernetes_service.vcluster_exposure.spec[0].port[0].node_port}" : try(coalesce(kubernetes_service.vcluster_exposure.status[0].load_balancer[0].ingress[0].ip, kubernetes_service.vcluster_exposure.status[0].load_balancer[0].ingress[0].hostname), "")
}

resource "helm_release" "vcluster" {
  name          = var.cluster_name
  namespace     = kubernetes_namespace.vcluster.metadata[0].name
  repository    = endswith(var.chart_name_or_path, ".tgz") ? null : var.chart_repository
  chart         = var.chart_name_or_path
  version       = endswith(var.chart_name_or_path, ".tgz") ? null : var.chart_version
  wait          = true
  wait_for_jobs = true

  values = [
    templatefile("${path.module}/values.yaml.tftpl", {
      endpoint     = local.external_endpoint
      cluster_name = var.cluster_name
    })
  ]

  depends_on = [
    kubernetes_service.vcluster_exposure,
    kubernetes_resource_quota.vcluster_quota,
    kubernetes_limit_range.vcluster_limits
  ]
}

data "kubernetes_secret" "vcluster_kubeconfig" {
  metadata {
    name      = "vc-${var.cluster_name}"
    namespace = kubernetes_namespace.vcluster.metadata[0].name
  }

  depends_on = [helm_release.vcluster]
}

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
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.0.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15.0"
    }
  }
}

# 1. Helm Release for External Secrets Operator (ESO)
resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = "0.9.11"
  namespace        = "external-secrets"
  create_namespace = true

  set {
    name  = "installCRDs"
    value = "true"
  }

  set {
    name  = "serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account"
    value = var.secret_rotation_sa_email
  }
}

# 3. Kubernetes Namespace Creation
resource "kubernetes_namespace_v1" "secret_rotation" {
  metadata {
    name = var.namespace
  }
}

# 4. Deploy Workloads via Helm Chart
resource "helm_release" "workloads" {
  name      = "workloads"
  chart     = "${path.module}/workloads-chart"
  namespace = kubernetes_namespace_v1.secret_rotation.metadata[0].name

  set {
    name  = "projectID"
    value = var.project_id
  }

  set {
    name  = "namespace"
    value = var.namespace
  }

  set {
    name  = "secretName"
    value = var.secret_id
  }

  depends_on = [helm_release.external_secrets]
}

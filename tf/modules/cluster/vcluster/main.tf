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
  host_context = var.host_context != "" ? var.host_context : "gke_${var.project_id}_${var.location}_${var.host_cluster_name}"
}

# No local `provider "helm"` / `provider "kubernetes"` blocks here: a module
# that declares its own provider configurations cannot be called with
# `count`, which the dispatch module (tf/modules/cluster) needs to select
# between gke/kind/vcluster. Instead this module only declares
# required_providers and relies on the default (unaliased) helm/kubernetes/
# google provider configurations passed down implicitly from its root
# caller (e.g. tf/prebuilt/vcluster), which points them at the host cluster.

# Reserving the IP up front lets it be baked into the proxy cert SANs and the
# exported kubeconfig before the vcluster Service exists, avoiding a
# TLS-SAN chicken-and-egg problem between the Service's LoadBalancer IP and
# the certificate the control plane serves.
resource "google_compute_address" "vcluster" {
  name   = "${var.cluster_name}-ip"
  region = var.location
}

# Managed explicitly (not via helm's create_namespace) so `tofu destroy`
# deletes the namespace, and with it the vcluster StatefulSet's PVC. A plain
# `helm uninstall` leaves the PVC behind, and a re-created vcluster resumes
# stale state from the old PVC.
resource "kubernetes_namespace" "vcluster" {
  metadata {
    name = var.cluster_name
  }
}

resource "helm_release" "vcluster" {
  name       = var.cluster_name
  repository = "https://charts.loft.sh"
  chart      = "vcluster"
  version    = var.vcluster_chart_version
  namespace  = kubernetes_namespace.vcluster.metadata[0].name

  create_namespace = false
  wait             = true
  timeout          = 900

  values = [
    templatefile("${path.module}/vcluster.yaml.tftpl", {
      lb_ip = google_compute_address.vcluster.address
    })
  ]
}

# Polls for the vcluster kubeconfig secret, writes it locally, then polls the
# vcluster API through that kubeconfig until it answers. Both steps can take
# a while after the Helm release reports ready: the LoadBalancer Service
# needs to program the reserved IP, and the vcluster API needs to come up
# behind it.
resource "terraform_data" "kubeconfig" {
  depends_on = [helm_release.vcluster]

  triggers_replace = {
    cluster_name = var.cluster_name
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e

      host_kubeconfig="${pathexpand(var.kubeconfig_path_host)}"
      host_context="${local.host_context}"
      namespace="${kubernetes_namespace.vcluster.metadata[0].name}"
      secret_name="vc-${var.cluster_name}"
      out_path="${pathexpand(var.kubeconfig_path)}"

      elapsed=0
      timeout=300
      interval=5
      until kubectl --kubeconfig="$host_kubeconfig" --context="$host_context" \
        -n "$namespace" get secret "$secret_name" >/dev/null 2>&1; do
        if [ "$elapsed" -ge "$timeout" ]; then
          echo "Timed out after $${timeout}s waiting for secret $secret_name in namespace $namespace" >&2
          exit 1
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
      done

      mkdir -p "$(dirname "$out_path")"
      kubectl --kubeconfig="$host_kubeconfig" --context="$host_context" \
        -n "$namespace" get secret "$secret_name" \
        --template='{{.data.config}}' | base64 -d > "$out_path"

      elapsed=0
      timeout=600
      until kubectl --kubeconfig="$out_path" get ns >/dev/null 2>&1; do
        if [ "$elapsed" -ge "$timeout" ]; then
          echo "Timed out after $${timeout}s waiting for the vcluster API to answer via $out_path" >&2
          exit 1
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
      done
    EOT
  }
}

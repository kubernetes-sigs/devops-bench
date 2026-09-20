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
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.0.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
  region  = var.location != "" && var.location != "local" ? var.location : null
}

provider "kind" {}

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

data "google_client_config" "default" {
  count = var.infra_provider == "gcp" ? 1 : 0
}

provider "kubernetes" {
  # managed_endpoint rather than endpoint: endpoint names the vcluster
  # submodule, and configuring this provider from it is a dependency cycle.
  host                   = var.infra_provider == "gcp" ? "https://${module.cluster.managed_endpoint}" : module.cluster.managed_endpoint
  token                  = var.infra_provider == "gcp" ? data.google_client_config.default[0].access_token : null
  client_certificate     = var.infra_provider == "kind" ? module.cluster.client_certificate : null
  client_key             = var.infra_provider == "kind" ? module.cluster.client_key : null
  cluster_ca_certificate = var.infra_provider == "gcp" ? base64decode(module.cluster.cluster_ca_certificate) : (var.infra_provider == "kind" ? module.cluster.cluster_ca_certificate : null)
}


# A stock kind cluster ships no metrics-server, and the HPA objective needs
# ScalingActive=True.
resource "null_resource" "metrics_server" {
  count = var.infra_provider == "kind" ? 1 : 0

  depends_on = [module.cluster]

  triggers = {
    cluster = module.cluster.cluster_name
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml"
      kubectl -n kube-system get deploy metrics-server -o json \
        | jq '(.spec.template.spec.containers[0].args) += ["--kubelet-insecure-tls"]' \
        | kubectl apply -f -
      kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s
    EOT

    environment = {
      KUBECONFIG = pathexpand(var.kubeconfig_path)
    }
  }
}

# The workload the agent must make surge-ready: no resources block and no HPA.
resource "kubernetes_deployment_v1" "target" {
  metadata {
    name      = var.target_deployment_name
    namespace = var.namespace
    labels = {
      app = var.target_deployment_name
    }
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        app = var.target_deployment_name
      }
    }

    template {
      metadata {
        labels = {
          app = var.target_deployment_name
        }
      }

      spec {
        container {
          name  = "web"
          image = "python:3.11-slim"

          # The load generator port-forwards to a fixed remote port 8080, so
          # the server must listen there or the spike never reaches it.
          command = ["python3", "-c"]
          args = [
            <<-PY
              import http.server, socketserver
              class Handler(http.server.BaseHTTPRequestHandler):
                  def do_GET(self):
                      total = 0
                      for i in range(3_000_000):
                          total += i * i
                      self.send_response(200)
                      self.end_headers()
                      self.wfile.write(b"ok\n")
                  def log_message(self, *a):
                      pass
              class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
                  allow_reuse_address = True
                  daemon_threads = True
              Server(("", 8080), Handler).serve_forever()
            PY
          ]

          port {
            container_port = 8080
          }
          # No resources block on purpose; adding requests and limits is the
          # agent's job.
        }
      }
    }
  }

  # The agent's HPA changes the replica count; a re-apply must not fight it.
  lifecycle {
    ignore_changes = [
      spec[0].replicas,
    ]
  }
}

resource "kubernetes_service_v1" "target" {
  metadata {
    name      = var.target_deployment_name
    namespace = var.namespace
  }

  spec {
    selector = {
      app = var.target_deployment_name
    }

    port {
      port        = 8080
      target_port = 8080
    }

    # LoadBalancer on GKE so the load generator can reach the Service directly;
    # ClusterIP on kind, where the harness falls back to a port-forward.
    type = var.infra_provider == "gcp" ? "LoadBalancer" : "ClusterIP"
  }

  wait_for_load_balancer = var.infra_provider == "gcp" ? true : false
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}

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

# GKE/KinD cluster. cluster_name is run-token-prefixed by the harness under parallel
# runs, so every concurrent run gets its own cluster — all in-cluster objects
# below are therefore collision-free without any name suffixing.
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
  host                   = var.infra_provider == "gcp" ? "https://${module.cluster.endpoint}" : module.cluster.endpoint
  token                  = var.infra_provider == "gcp" ? data.google_client_config.default[0].access_token : null
  client_certificate     = var.infra_provider == "kind" ? module.cluster.client_certificate : null
  client_key             = var.infra_provider == "kind" ? module.cluster.client_key : null
  cluster_ca_certificate = var.infra_provider == "gcp" ? base64decode(module.cluster.cluster_ca_certificate) : (var.infra_provider == "kind" ? module.cluster.cluster_ca_certificate : null)
}


# Pre-seeded target workload the agent must optimize. It is deliberately
# misconfigured for autoscaling: NO resource requests/limits and NO HPA. The
# task asks the agent to add requests/limits, create an HPA (minReplicas > 1),
# and survive a load spike. The app + service are named
# ${var.target_deployment_name} so the prompt / chaos service_url /
# verification placeholders resolve to it:
#   http://{{TARGET_DEPLOYMENT_NAME}}.{{NAMESPACE}}.svc.cluster.local
#
# Image: registry.k8s.io/hpa-example — the canonical CPU-burn app from the
# Kubernetes HPA walkthrough; each HTTP request consumes CPU, so generated load
# drives CPU up and a correctly-configured HPA scales out.
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

          # CPU-burn HTTP server on port 8080. The chaos harness port-forwards
          # `deployment/<target>` to a FIXED remote port 8080
          # (devops_bench/chaos/faults/generate_load.py:_LOCAL_PORT, passed as
          # remote_port), so the workload MUST listen on 8080 or the generated
          # load never reaches it. (registry.k8s.io/hpa-example listens on :80 and
          # would silently drop the spike — the run only "passes" because the
          # agent's HPA minReplicas scales it independent of load.)
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
          # No resources block on purpose: adding requests/limits is the agent's
          # job. Resource-based HPA cannot target CPU without requests set.
        }
      }
    }
  }

  # The HPA the agent creates changes the replica count; ignore it so any
  # re-apply does not fight the agent's autoscaling.
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

    # External LoadBalancer so the chaos load fault can reach the workload from
    # any runner (in-VPC bastion or off-VPC local) without a port-forward, which
    # drops connections under sustained 300 qps. GKE auto-provisions a network
    # LB with an external IP and an ALLOW firewall rule on the default VPC; the
    # harness resolves status.loadBalancer.ingress[0].ip and points the load at
    # http://<ip>:8080 directly.
    #
    # On KinD, we use ClusterIP and rely on the harness port-forward fallback,
    # avoiding a pending external IP wait.
    type = var.infra_provider == "gcp" ? "LoadBalancer" : "ClusterIP"
  }

  # Wait for the LB IP to be assigned before terraform returns, so the harness
  # can resolve the external IP immediately after apply. Only applicable to GKE.
  wait_for_load_balancer = var.infra_provider == "gcp" ? true : false
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "cluster_location" {
  value = module.cluster.location
}

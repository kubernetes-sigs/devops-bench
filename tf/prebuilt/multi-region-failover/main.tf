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
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region_primary
}

locals {
  # GitOps repo path on the shared bastion host. Per-run unique (cluster_name is
  # run-token-prefixed) so concurrent / back-to-back runs don't rm -rf + reseed
  # each other's repo. setup.sh rm -rf's this path, so a fixed default would let
  # one run wipe another's source of truth. The task prompt references the same
  # path via the {{CLUSTER_NAME}} placeholder, which the harness resolves from
  # this stack's `cluster_name` OUTPUT -- i.e. the EAST cluster's finalized name,
  # not var.cluster_name. The default is therefore built from local.east_cluster;
  # building it from var.cluster_name would hand the agent a path that does not
  # exist. An explicit var.repo_path wins.
  repo_path = var.repo_path != "" ? var.repo_path : "~/app-repo-${local.east_cluster}.git"

  # Host-side, west-only kubeconfig written by setup.sh. The harness credentials
  # exactly one cluster -- whatever `cluster_name` resolves to, which is east --
  # so verifiers that need to read the STANDBY have no context to reach it
  # through. This file gives them one; the task's verification_spec points its
  # `kubeconfig:` at the same path, spelled with {{CLUSTER_NAME}}. It lives
  # outside $HOME deliberately, so a run that quarantines HOME does not hide it
  # from the verifier, and it is removed on destroy.
  west_kubeconfig = "/var/tmp/devops-bench/${local.east_cluster}-west.kubeconfig"

  # Region-prefixed cluster names. The discriminator must land in the FIRST 15
  # chars: tf/modules/cluster/gke derives the node SA account_id from
  # substr(cluster_name, 0, 15), so a "-east"/"-west" *suffix* (past char 15)
  # would give both clusters the SAME account_id and collide on a single apply.
  # A leading "e-"/"w-" keeps the run token in-window (cross-run unique) while
  # distinguishing the two clusters. (cluster_name is already clamped to 40, and
  # "multi-region-failover" leaves ample room for the 2-char prefix.)
  east_cluster = "e-${var.cluster_name}"
  west_cluster = "w-${var.cluster_name}"
}

# Cloud SQL instance names cannot be reused for ~1 week after deletion, which breaks
# back-to-back eval runs. A random suffix sidesteps the collision on every apply.
resource "random_id" "suffix" {
  byte_length = 3
}

# ---------------------------------------------------------------------------
# Two regional (zonal) GKE clusters: east = primary, west = standby.
# ---------------------------------------------------------------------------
module "east" {
  source                = "../../modules/cluster"
  infra_provider        = "gcp"
  project_id            = var.project_id
  cluster_name          = local.east_cluster
  location              = var.zone_primary
  node_count            = var.node_count_primary
  machine_type          = var.machine_type
  # BYO-credentials model (see docs/bastion.md): the agent runs as the operator's
  # broad bastion VM SA, which already holds container.admin out-of-band. This
  # stack grants NOTHING — a per-run stack must not manage a project IAM binding
  # on a SHARED principal, because one run's `tofu destroy` would revoke the
  # binding a concurrent run still needs (teardown contention).
  agent_service_account = ""
}

module "west" {
  source                = "../../modules/cluster"
  infra_provider        = "gcp"
  project_id            = var.project_id
  cluster_name          = local.west_cluster
  location              = var.zone_standby
  node_count            = var.node_count_standby
  machine_type          = var.machine_type
  agent_service_account = ""
}

# ---------------------------------------------------------------------------
# Reserved external IPs. The regional IPs are assigned to each cluster's frontend
# Service (loadBalancerIP) so the global LB's internet NEGs can target known IPs.
# ---------------------------------------------------------------------------
resource "google_compute_address" "east_ip" {
  name   = "fe-east-${var.cluster_name}-${random_id.suffix.hex}"
  region = var.region_primary
}

resource "google_compute_address" "west_ip" {
  name   = "fe-west-${var.cluster_name}-${random_id.suffix.hex}"
  region = var.region_standby
}

resource "google_compute_global_address" "lb_ip" {
  name = "storefront-lb-${var.cluster_name}-${random_id.suffix.hex}"
}

# ---------------------------------------------------------------------------
# Cross-region Cloud SQL: primary in east, read replica in west. The agent checks
# replication health/lag here before deciding it is safe to fail over.
# ---------------------------------------------------------------------------
resource "google_sql_database_instance" "primary" {
  name                = "storefront-${var.cluster_name}-${random_id.suffix.hex}"
  database_version    = "MYSQL_8_0"
  region              = var.region_primary
  deletion_protection = false

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    backup_configuration {
      enabled            = true
      binary_log_enabled = true # required to source a read replica
    }
  }
}

resource "google_sql_database_instance" "replica" {
  name                 = "storefront-${var.cluster_name}-replica-${random_id.suffix.hex}"
  database_version     = "MYSQL_8_0"
  region               = var.region_standby
  master_instance_name = google_sql_database_instance.primary.name
  deletion_protection  = false

  replica_configuration {
    failover_target = false
  }

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
  }

  depends_on = [google_sql_database_instance.primary]
}

resource "google_sql_database" "app" {
  name     = "storefront"
  instance = google_sql_database_instance.primary.name
}

resource "google_sql_user" "app" {
  name     = "storefront"
  instance = google_sql_database_instance.primary.name
  password = "storefront-${random_id.suffix.hex}"
}

# ---------------------------------------------------------------------------
# Global external HTTP load balancer fronting both regions via internet NEGs.
# The URL map default_service is pinned to EAST; when east goes down the agent must
# re-point it to WEST (there is no automatic cross-backend-service failover).
# ---------------------------------------------------------------------------
resource "google_compute_global_network_endpoint_group" "east" {
  name                  = "neg-east-${var.cluster_name}-${random_id.suffix.hex}"
  network_endpoint_type = "INTERNET_IP_PORT"
  default_port          = 80
}

resource "google_compute_global_network_endpoint" "east" {
  global_network_endpoint_group = google_compute_global_network_endpoint_group.east.name
  ip_address                    = google_compute_address.east_ip.address
  port                          = 80
}

resource "google_compute_global_network_endpoint_group" "west" {
  name                  = "neg-west-${var.cluster_name}-${random_id.suffix.hex}"
  network_endpoint_type = "INTERNET_IP_PORT"
  default_port          = 80
}

resource "google_compute_global_network_endpoint" "west" {
  global_network_endpoint_group = google_compute_global_network_endpoint_group.west.name
  ip_address                    = google_compute_address.west_ip.address
  port                          = 80
}

# Health checks are not supported on internet-NEG backends, so they are omitted; the
# agent detects the outage from the 5xx error rate, not from LB health state.
resource "google_compute_backend_service" "east" {
  name                  = "be-east-${var.cluster_name}-${random_id.suffix.hex}"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL"
  timeout_sec           = 10

  backend {
    group = google_compute_global_network_endpoint_group.east.id
  }
}

resource "google_compute_backend_service" "west" {
  name                  = "be-west-${var.cluster_name}-${random_id.suffix.hex}"
  protocol              = "HTTP"
  load_balancing_scheme = "EXTERNAL"
  timeout_sec           = 10

  backend {
    group = google_compute_global_network_endpoint_group.west.id
  }
}

resource "google_compute_url_map" "lb" {
  name = "storefront-urlmap-${var.cluster_name}-${random_id.suffix.hex}"
  # Pinned to the primary region. The agent's failover action is to set this to the
  # west backend service (e.g. `gcloud compute url-maps set-default-service`).
  default_service = google_compute_backend_service.east.id
}

resource "google_compute_target_http_proxy" "lb" {
  name    = "storefront-proxy-${var.cluster_name}-${random_id.suffix.hex}"
  url_map = google_compute_url_map.lb.id
}

resource "google_compute_global_forwarding_rule" "lb" {
  name                  = "storefront-fr-${var.cluster_name}-${random_id.suffix.hex}"
  target                = google_compute_target_http_proxy.lb.id
  ip_address            = google_compute_global_address.lb_ip.address
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL"
}

# ---------------------------------------------------------------------------
# Outside-the-cluster setup: deploy the app to both clusters, inject the regional
# outage in east, leave west missing the replicated config, seed the GitOps repo.
# ---------------------------------------------------------------------------
resource "null_resource" "setup" {
  triggers = {
    east_cluster    = module.east.cluster_name
    west_cluster    = module.west.cluster_name
    east_ip         = google_compute_address.east_ip.address
    west_ip         = google_compute_address.west_ip.address
    west_kubeconfig = local.west_kubeconfig
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "${path.module}/scripts/setup.sh"

    environment = {
      PROJECT_ID      = var.project_id
      NAMESPACE       = var.namespace
      EAST_CLUSTER    = module.east.cluster_name
      EAST_ZONE       = var.zone_primary
      WEST_CLUSTER    = module.west.cluster_name
      WEST_ZONE       = var.zone_standby
      EAST_IP         = google_compute_address.east_ip.address
      WEST_IP         = google_compute_address.west_ip.address
      LB_IP           = google_compute_global_address.lb_ip.address
      REPO_PATH       = local.repo_path
      SQL_PRIMARY     = google_sql_database_instance.primary.name
      SQL_REPLICA     = google_sql_database_instance.replica.name
      MANIFESTS_DIR   = "${path.module}/manifests"
      WEST_KUBECONFIG = local.west_kubeconfig
    }
  }

  # Destroy-time provisioners may only reference `self`, hence the trigger above.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "rm -f '${self.triggers.west_kubeconfig}'"
  }

  depends_on = [
    module.east,
    module.west,
    google_sql_database_instance.replica,
    google_compute_global_forwarding_rule.lb,
  ]
}

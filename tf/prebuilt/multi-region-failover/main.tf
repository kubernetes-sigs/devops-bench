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
  # Built from local.east_cluster because the prompt's {{CLUSTER_NAME}}
  # resolves from this stack's cluster_name output, which is the east cluster.
  # Per-run unique because setup.sh removes and reseeds the path.
  repo_path = var.repo_path != "" ? var.repo_path : "~/app-repo-${local.east_cluster}.git"

  # West-only kubeconfig written by setup.sh. The harness credentials only the
  # east cluster, so verifiers that read the standby point their kubeconfig:
  # at this path; the task's verification_spec spells the same path with
  # {{CLUSTER_NAME}}. Outside $HOME so a run that quarantines HOME still sees it.
  west_kubeconfig = "/var/tmp/devops-bench/${local.east_cluster}-west.kubeconfig"

  # The region marker is a prefix because the cluster module derives the node
  # SA account_id from the first 15 characters of the name; a suffix would give
  # both clusters the same account_id.
  east_cluster = "e-${var.cluster_name}"
  west_cluster = "w-${var.cluster_name}"
}

# Cloud SQL instance names cannot be reused for about a week after deletion.
resource "random_id" "suffix" {
  byte_length = 3
}

# east = primary, west = standby.
module "east" {
  source         = "../../modules/cluster"
  infra_provider = "gcp"
  project_id     = var.project_id
  cluster_name   = local.east_cluster
  location       = var.zone_primary
  node_count     = var.node_count_primary
  machine_type   = var.machine_type
  # The agent's service account already holds container.admin. A per-run stack
  # must not manage a project IAM binding on a shared principal, because one
  # run's destroy would revoke the binding a concurrent run still needs.
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

# Regional IPs are assigned to each cluster's frontend Service so the global
# load balancer's internet NEGs can target known addresses.
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

# Cloud SQL primary in east, read replica in west.
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

# Global external HTTP load balancer fronting both regions via internet NEGs.
# There is no automatic failover between backend services.
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

# Health checks are not supported on internet-NEG backends, so the outage
# shows up as a 5xx rate rather than as load balancer health state.
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
  # Pinned to the primary region; failing over means re-pointing this.
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
      # setup.sh reads $HOME under set -u; a local-exec only inherits what
      # the caller had.
      HOME = pathexpand("~")
    }
  }

  # Destroy-time provisioners may only reference self, hence the trigger above.
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

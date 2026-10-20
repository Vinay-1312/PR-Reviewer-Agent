# PR review bot on GCP: receiver (public) -> Pub/Sub -> worker (private) -> Gemini -> GitHub review
#
# Usage (two phases, because secrets and images must exist before Cloud Run can start):
#   Phase 1:  terraform init && terraform apply -var project_id=YOUR_PROJECT          # deploy_services=false
#   Then:     add secret versions + build/push images (see README steps in chat)
#   Phase 2:  terraform apply -var project_id=YOUR_PROJECT -var deploy_services=true \
#                             -var receiver_image=... -var worker_image=... -var github_app_id=...

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  type        = string
  description = "Existing GCP project with billing enabled"
}

variable "region" {
  type    = string
  default = "us-central1" # an Always Free eligible region
}

variable "deploy_services" {
  type        = bool
  default     = false
  description = "Set true only after secret versions exist and images are pushed"
}

variable "receiver_image" {
  type    = string
  default = ""
}

variable "worker_image" {
  type    = string
  default = ""
}

variable "github_app_id" {
  type    = string
  default = ""
}

variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account ID (e.g. 0123AB-456CDE-789FGH). Leave empty to skip the budget alert."
}

variable "budget_amount" {
  type    = number
  default = 5
}

variable "budget_currency" {
  type        = string
  default     = "USD"
  description = "Must match your billing account currency (e.g. GBP)"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

# ---------- APIs ----------
locals {
  apis = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "billingbudgets.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------- Service accounts ----------
resource "google_service_account" "receiver" {
  account_id   = "receiver-sa"
  display_name = "PR webhook receiver"
}

resource "google_service_account" "worker" {
  account_id   = "worker-sa"
  display_name = "PR review worker"
}

resource "google_service_account" "pubsub_invoker" {
  account_id   = "pubsub-invoker"
  display_name = "Pub/Sub push invoker"
}

# ---------- Image registry ----------
resource "google_artifact_registry_repository" "images" {
  repository_id = "pr-review"
  format        = "DOCKER"
  location      = var.region
  depends_on    = [google_project_service.apis]
}

# ---------- Queue ----------
resource "google_pubsub_topic" "pr_events" {
  name       = "pr-events"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic_iam_member" "receiver_publish" {
  topic  = google_pubsub_topic.pr_events.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.receiver.email}"
}

# ---------- Secrets (containers only: add values with gcloud so they stay out of Terraform state) ----------
resource "google_secret_manager_secret" "webhook_secret" {
  secret_id  = "github-webhook-secret"
  depends_on = [google_project_service.apis]
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "app_private_key" {
  secret_id  = "github-app-private-key"
  depends_on = [google_project_service.apis]
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "gemini_key" {
  secret_id  = "gemini-api-key"
  depends_on = [google_project_service.apis]
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "receiver_webhook" {
  secret_id = google_secret_manager_secret.webhook_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_private_key" {
  secret_id = google_secret_manager_secret.app_private_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_gemini" {
  secret_id = google_secret_manager_secret.gemini_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# ---------- Firestore (dedupe / review state) ----------
resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

resource "google_project_iam_member" "worker_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# ---------- Receiver (public; protected by webhook signature check) ----------
resource "google_cloud_run_v2_service" "receiver" {
  count               = var.deploy_services ? 1 : 0
  name                = "pr-receiver"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.receiver.email
    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }
    containers {
      image = var.receiver_image
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "TOPIC"
        value = google_pubsub_topic.pr_events.name
      }
      env {
        name = "GITHUB_WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.webhook_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.receiver_webhook,
    google_pubsub_topic_iam_member.receiver_publish,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "receiver_public" {
  count    = var.deploy_services ? 1 : 0
  name     = google_cloud_run_v2_service.receiver[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------- Worker (private; only Pub/Sub can call it) ----------
resource "google_cloud_run_v2_service" "worker" {
  count               = var.deploy_services ? 1 : 0
  name                = "pr-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.worker.email
    timeout                          = "300s"
    max_instance_request_concurrency = 1
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = var.worker_image
      resources {
        limits = {
          memory = "512Mi"
          cpu    = "1"
        }
      }
      env {
        name  = "GITHUB_APP_ID"
        value = var.github_app_id
      }
      env {
        name = "GITHUB_APP_PRIVATE_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_private_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.worker_private_key,
    google_secret_manager_secret_iam_member.worker_gemini,
    google_firestore_database.default,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "worker_invoker" {
  count    = var.deploy_services ? 1 : 0
  name     = google_cloud_run_v2_service.worker[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

resource "google_pubsub_subscription" "pr_events_push" {
  count                = var.deploy_services ? 1 : 0
  name                 = "pr-events-push"
  topic                = google_pubsub_topic.pr_events.id
  ack_deadline_seconds = 300

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.worker[0].uri}/pubsub"
    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.worker[0].uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  depends_on = [google_cloud_run_v2_service_iam_member.worker_invoker]
}

# ---------- Budget alert (emails billing admins by default) ----------
resource "google_billing_budget" "budget" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "pr-review-bot"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.apis]
}

# ---------- Outputs ----------
output "image_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "receiver_url" {
  value = var.deploy_services ? google_cloud_run_v2_service.receiver[0].uri : null
}

output "worker_url" {
  value = var.deploy_services ? google_cloud_run_v2_service.worker[0].uri : null
}

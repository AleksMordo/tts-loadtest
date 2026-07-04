# Стенд: GPU-ВМ (TTS) + CPU-ВМ (генератор нагрузки + Prometheus) в одной подсети,
# чтобы исключить внешнюю сеть из измерений. Наружу — только SSH с указанного IP.

data "yandex_compute_image" "base" {
  family = var.image_family
}

resource "yandex_vpc_network" "loadtest" {
  name = "tts-loadtest"
}

resource "yandex_vpc_subnet" "loadtest" {
  name           = "tts-loadtest"
  zone           = var.zone
  network_id     = yandex_vpc_network.loadtest.id
  v4_cidr_blocks = [var.subnet_cidr]
}

resource "yandex_vpc_security_group" "loadtest" {
  name       = "tts-loadtest"
  network_id = yandex_vpc_network.loadtest.id

  # Внутри подсети всё открыто
  ingress {
    protocol       = "ANY"
    description    = "internal"
    v4_cidr_blocks = [var.subnet_cidr]
  }

  # Наружу принимаем только SSH с моего IP
  ingress {
    protocol       = "TCP"
    description    = "ssh"
    port           = 22
    v4_cidr_blocks = [var.allowed_ssh_cidr]
  }

  egress {
    protocol       = "ANY"
    description    = "outbound (docker pull, apt, registry)"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "yandex_compute_instance" "gpu" {
  name        = "tts-gpu"
  platform_id = var.gpu_platform_id
  zone        = var.zone

  resources {
    cores  = var.gpu_vm_cores
    memory = var.gpu_vm_memory_gb
    gpus   = var.gpu_count
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.base.id
      size     = var.gpu_disk_gb
      type     = "network-ssd"
    }
  }

  network_interface {
    subnet_id          = yandex_vpc_subnet.loadtest.id
    nat                = true # публичный IP для SSH/деплоя
    security_group_ids = [yandex_vpc_security_group.loadtest.id]
  }

  scheduling_policy {
    preemptible = var.preemptible
  }

  metadata = {
    ssh-keys = "${var.ssh_user}:${file(var.ssh_public_key_path)}"
  }
}

# Генератор нагрузки + мониторинг (Prometheus) совмещены на одной CPU-ВМ
resource "yandex_compute_instance" "loadgen" {
  name        = "tts-loadgen"
  platform_id = "standard-v3"
  zone        = var.zone

  resources {
    cores  = var.loadgen_cores
    memory = var.loadgen_memory_gb
  }

  boot_disk {
    initialize_params {
      image_id = data.yandex_compute_image.base.id
      size     = var.loadgen_disk_gb
      type     = "network-ssd"
    }
  }

  network_interface {
    subnet_id          = yandex_vpc_subnet.loadtest.id
    nat                = true
    security_group_ids = [yandex_vpc_security_group.loadtest.id]
  }

  scheduling_policy {
    preemptible = var.preemptible
  }

  metadata = {
    ssh-keys = "${var.ssh_user}:${file(var.ssh_public_key_path)}"
  }
}

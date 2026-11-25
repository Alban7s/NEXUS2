# Ansible - configure the NEXUS Ubuntu 24.04 server

Installs/configures: system updates and base packages, UFW firewall, **Java 21**, **Docker Engine**
(with log rotation + weekly prune for the 25 GB disk), **Trivy**, **Jenkins** (bound to localhost),
**kubectl**, **Minikube** prerequisites (optional start), and optionally the **monitoring stack**.

## Setup (on your laptop / WSL)

```bash
sudo apt install ansible            # or: pip install ansible
ansible-galaxy collection install community.general
cp inventory/hosts.example.yml inventory/hosts.yml     # edit the IP + key path (no passwords)
ansible all -m ping                                    # connectivity test
```

## Validate and run

```bash
ansible-playbook site.yml --syntax-check     # 1. syntax only (changes nothing)
ansible-playbook site.yml --check --diff     # 2. dry run: shows what WOULD change
ansible-playbook site.yml                    # 3. real run (changes the server)
ansible-playbook site.yml --tags docker      # run one role
ansible-playbook site.yml -e minikube_start=true -e monitoring_deploy=true
```

Running it twice should report `changed=0` the second time (idempotent). Check mode is a
good approximation but a few tasks (e.g. repository keys) cannot be fully simulated.

> Never run the real play against a server you have not been authorised to change.

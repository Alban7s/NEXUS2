// NEXUS platform self-deployment pipeline.
// Configure this root Jenkinsfile as "Pipeline script from SCM" in Jenkins.
// Required Jenkins credentials:
//   docker_creds   = Username with password (Docker Hub username + access token)
//   kubeconfig     = Secret file (kubectl config view --raw --flatten --minify)
//   nexus-api-key  = Secret text (same value as NEXUS_API_KEY)
//   docker-client-certs = Secret file (tar.gz with ca.pem, cert.pem, key.pem)
// Required agent tools: git, Python 3.10+, Docker CLI/daemon, kubectl, Trivy, curl.
// Jenkins is the CI/CD orchestrator; Terraform/Ansible provision the host separately.

pipeline {
  // Use a trusted administrator-managed Linux agent with Docker access.
  // Do not run untrusted pull-request code on this root-equivalent executor.
  agent { label 'linux-docker' }

  options {
    timestamps()
    timeout(time: 60, unit: 'MINUTES')
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '20'))
    skipDefaultCheckout(true)
  }

  triggers {
    // Works even when Jenkins is kept private behind SSH. Configure a GitHub
    // webhook as an additional trigger only if a secured public endpoint exists.
    pollSCM('H/5 * * * *')
  }

  parameters {
    string(name: 'DOCKERHUB_REPO', defaultValue: 'your-dockerhub-username/nexus-platform',
      description: 'Docker Hub repository in namespace/repository form (not a token).')
    choice(name: 'TRIVY_SEVERITY', choices: ['CRITICAL', 'HIGH,CRITICAL', 'MEDIUM,HIGH,CRITICAL', 'NONE'],
      description: 'Severity threshold. NONE reports findings without blocking deployment.')
    choice(name: 'K8S_MANIFEST', choices: ['k8s/nexus-local.yaml', 'k8s/nexus-eks.yaml'],
      description: 'Choose Minikube or EKS deployment manifest.')
  }

  environment {
    K8S_NAMESPACE = 'nexus'
    K8S_DEPLOYMENT = 'nexus'
    K8S_SERVICE = 'nexus'
    DOCKER_BUILDKIT = '1'
  }

  stages {
    stage('Checkout NEXUS') {
      steps {
        checkout scm
        script {
          env.GIT_SHA = sh(script: 'git rev-parse --short=12 HEAD', returnStdout: true).trim()
          env.IMAGE_TAG = "${env.GIT_SHA}-${env.BUILD_NUMBER}"
          if (!(params.DOCKERHUB_REPO ==~ /^[a-z0-9][a-z0-9._-]{0,99}\/[a-z0-9][a-z0-9._-]{0,99}$/)) {
            error('DOCKERHUB_REPO must be a Docker Hub namespace/repository, e.g. myuser/nexus-platform.')
          }
          env.IMAGE = "docker.io/${params.DOCKERHUB_REPO}:${env.IMAGE_TAG}"
          env.IMAGE_LATEST = "docker.io/${params.DOCKERHUB_REPO}:latest"
        }
        echo "Checked out ${env.GIT_SHA}; target image ${env.IMAGE}"
      }
    }

    stage('Validate build agent') {
      steps {
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          for tool in python3 git docker kubectl trivy curl; do
            command -v "$tool" >/dev/null || { echo "Required tool missing on Jenkins agent: $tool"; exit 2; }
          done
          python3 --version
          docker version
          kubectl version --client
          trivy --version
        '''
      }
    }

    stage('Test') {
      steps {
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          python3 -m venv .ci-venv
          . .ci-venv/bin/activate
          python -m pip install --upgrade pip
          pip install -r requirements-dev.txt
          python -m compileall -q backend tests
          python -m pytest backend/tests -q
          python tests/stubbed_handlers.py
        '''
      }
    }

    stage('Build NEXUS image') {
      steps {
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          docker build --pull --build-arg INSTALL_TRIVY=true \
            --label "org.opencontainers.image.revision=${GIT_SHA}" \
            --label "org.opencontainers.image.source=${GIT_URL:-unknown}" \
            -t "$IMAGE" -t "$IMAGE_LATEST" .
        '''
      }
    }

    stage('Trivy image scan') {
      steps {
        script {
          def exitCode = params.TRIVY_SEVERITY == 'NONE' ? '0' : '1'
          def severityFilter = params.TRIVY_SEVERITY == 'NONE' ? 'CRITICAL,HIGH,MEDIUM,LOW' : params.TRIVY_SEVERITY
          int scanStatus = withEnv(["TRIVY_EXIT_CODE=${exitCode}", "TRIVY_SEVERITY_FILTER=${severityFilter}"]) {
            sh(script: '''#!/usr/bin/env bash
              set -euo pipefail
              trivy image --scanners vuln,misconfig,secret --severity "$TRIVY_SEVERITY_FILTER" \
                --ignore-unfixed --exit-code "$TRIVY_EXIT_CODE" --format json --output nexus-trivy.json "$IMAGE"
            ''', returnStatus: true)
          }
          archiveArtifacts artifacts: 'nexus-trivy.json', allowEmptyArchive: true, fingerprint: true
          if (scanStatus != 0) {
            error("Trivy found findings at or above ${params.TRIVY_SEVERITY}; image was not pushed.")
          }
        }
      }
    }

    stage('Push to Docker Hub') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'docker_creds',
          usernameVariable: 'DOCKER_USER', passwordVariable: 'DOCKER_PASS')]) {
          sh '''#!/usr/bin/env bash
            set -euo pipefail
            set +x
            export DOCKER_CONFIG="$(mktemp -d)"
            chmod 700 "$DOCKER_CONFIG"
            trap 'docker logout >/dev/null 2>&1 || true; rm -rf "$DOCKER_CONFIG"' EXIT
            printf '%s' "$DOCKER_PASS" | docker login --username "$DOCKER_USER" --password-stdin
            docker push "$IMAGE"
            docker push "$IMAGE_LATEST"
          '''
        }
      }
    }

    stage('Deploy to Kubernetes') {
      steps {
        withCredentials([
          file(credentialsId: 'kubeconfig', variable: 'KUBECONFIG_FILE'),
          usernamePassword(credentialsId: 'docker_creds', usernameVariable: 'DOCKER_USER', passwordVariable: 'DOCKER_PASS'),
          string(credentialsId: 'nexus-api-key', variable: 'NEXUS_API_KEY'),
          file(credentialsId: 'docker-client-certs', variable: 'DOCKER_CLIENT_CERTS')
        ]) {
          sh '''#!/usr/bin/env bash
            set -euo pipefail
            set +x
            export KUBECONFIG="$KUBECONFIG_FILE"
            kubectl cluster-info
            kubectl get nodes

            kubectl apply -f k8s/namespace.yaml
            kubectl apply -f k8s/rbac.yaml

            # Build an ephemeral Docker config and store it as a pull secret.
            # The token is never written to the workspace or printed in logs.
            export DOCKER_CONFIG="$(mktemp -d)"
            KEY_FILE="$(mktemp)"
            DOCKER_CERT_DIR="$(mktemp -d)"
            REGISTRY_URL_FILE="$(mktemp)"
            REGISTRY_USER_FILE="$(mktemp)"
            REGISTRY_PASS_FILE="$(mktemp)"
            chmod 700 "$DOCKER_CONFIG" "$DOCKER_CERT_DIR"
            chmod 600 "$KEY_FILE" "$REGISTRY_URL_FILE" "$REGISTRY_USER_FILE" "$REGISTRY_PASS_FILE"
            trap 'rm -rf "$DOCKER_CONFIG" "$DOCKER_CERT_DIR"; rm -f "$KEY_FILE" "$REGISTRY_URL_FILE" "$REGISTRY_USER_FILE" "$REGISTRY_PASS_FILE"' EXIT
            printf '%s' "$DOCKER_PASS" > "$KEY_FILE"
            printf '%s' "$DOCKER_PASS" | docker login --username "$DOCKER_USER" --password-stdin >/dev/null
            kubectl create secret generic nexus-registry --namespace "$K8S_NAMESPACE" \\
              --type=kubernetes.io/dockerconfigjson \\
              --from-file=.dockerconfigjson="$DOCKER_CONFIG/config.json" \\
              --dry-run=client -o yaml | kubectl apply -f -

            # NEXUS also needs publisher credentials for customer application images.
            # This is separate from the Kubernetes imagePullSecret above.
            REGISTRY_NAMESPACE="${DOCKERHUB_REPO%%/*}"
            printf 'docker.io/%s' "$REGISTRY_NAMESPACE" > "$REGISTRY_URL_FILE"
            printf '%s' "$DOCKER_USER" > "$REGISTRY_USER_FILE"
            printf '%s' "$DOCKER_PASS" > "$REGISTRY_PASS_FILE"
            kubectl create secret generic nexus-registry-push --namespace "$K8S_NAMESPACE" \
              --from-file=REGISTRY_URL="$REGISTRY_URL_FILE" \
              --from-file=REGISTRY_USERNAME="$REGISTRY_USER_FILE" \
              --from-file=REGISTRY_PASSWORD="$REGISTRY_PASS_FILE" \
              --dry-run=client -o yaml | kubectl apply -f -

            printf '%s' "$NEXUS_API_KEY" > "$KEY_FILE"
            kubectl create secret generic nexus-runtime --namespace "$K8S_NAMESPACE" \\
              --from-file=NEXUS_API_KEY="$KEY_FILE" \\
              --dry-run=client -o yaml | kubectl apply -f -

            # The Minikube NEXUS Pod needs mTLS to reach the host Docker daemon.
            # Validate the administrator-provided tar archive and extract only
            # the required regular files; never trust arbitrary archive paths.
            if [ "$K8S_MANIFEST" = "k8s/nexus-local.yaml" ]; then
              python3 jenkins/scripts/extract-docker-client-certs.py "$DOCKER_CLIENT_CERTS" "$DOCKER_CERT_DIR"
              kubectl create secret generic nexus-docker-client --namespace "$K8S_NAMESPACE" \
                --from-file=ca.pem="$DOCKER_CERT_DIR/ca.pem" \
                --from-file=cert.pem="$DOCKER_CERT_DIR/cert.pem" \
                --from-file=key.pem="$DOCKER_CERT_DIR/key.pem" \
                --dry-run=client -o yaml | kubectl apply -f -
            fi

            # Replace only the image variable; no token/secret is in the manifest.
            python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text().replace("${NEXUS_IMAGE}", sys.argv[2]))' "$K8S_MANIFEST" "$IMAGE" | kubectl apply -f -
            kubectl annotate deployment "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" \\
              "kubernetes.io/change-cause=Jenkins build ${BUILD_NUMBER}, commit ${GIT_SHA}" --overwrite

            if ! kubectl rollout status "deployment/$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" --timeout=300s; then
              REVISION="$(kubectl get deployment "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" \\
                -o jsonpath='{.metadata.annotations.deployment\\.kubernetes\\.io/revision}' 2>/dev/null || echo 1)"
              if [ "${REVISION:-1}" -gt 1 ]; then
                echo 'New rollout failed; attempting verified Kubernetes rollback.'
                kubectl rollout undo "deployment/$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE"
                kubectl rollout status "deployment/$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" --timeout=300s
              fi
              exit 1
            fi

            READY="$(kubectl get deployment "$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" \\
              -o jsonpath='{.status.readyReplicas}')"
            [ "${READY:-0}" -ge 1 ] || { echo 'Deployment has no ready replicas.'; exit 1; }
            kubectl get pods -n "$K8S_NAMESPACE" -l app=nexus -o wide
            kubectl get service "$K8S_SERVICE" -n "$K8S_NAMESPACE"
          '''
        }
      }
    }

    stage('Verify application health') {
      steps {
        withCredentials([file(credentialsId: 'kubeconfig', variable: 'KUBECONFIG_FILE')]) {
          sh '''#!/usr/bin/env bash
            set -euo pipefail
            export KUBECONFIG="$KUBECONFIG_FILE"
            PF_LOG="$(mktemp)"
            kubectl port-forward -n "$K8S_NAMESPACE" "service/$K8S_SERVICE" 18080:8000 >"$PF_LOG" 2>&1 &
            PF_PID=$!
            cleanup() { kill "$PF_PID" >/dev/null 2>&1 || true; rm -f "$PF_LOG"; }
            trap cleanup EXIT
            OK=0
            for attempt in $(seq 1 30); do
              if curl --silent --show-error --fail http://127.0.0.1:18080/healthz >/dev/null; then
                OK=1
                break
              fi
              sleep 2
            done
            [ "$OK" -eq 1 ] || { cat "$PF_LOG"; echo 'NEXUS health endpoint did not return HTTP 200.'; exit 1; }
            echo 'NEXUS /healthz returned HTTP 200.'
            kubectl rollout status "deployment/$K8S_DEPLOYMENT" -n "$K8S_NAMESPACE" --timeout=60s
          '''
        }
      }
    }
  }

  post {
    success {
      echo "NEXUS deployment succeeded: ${env.IMAGE}"
    }
    failure {
      echo 'NEXUS pipeline failed. Inspect the failed stage and Kubernetes pod events before retrying.'
    }
    always {
      sh 'rm -rf .ci-venv || true'
      sh 'if [ -n \"${IMAGE:-}\" ]; then docker image rm -f \"$IMAGE\" \"${IMAGE_LATEST:-}\" >/dev/null 2>&1 || true; fi'
      deleteDir()
    }
  }
}

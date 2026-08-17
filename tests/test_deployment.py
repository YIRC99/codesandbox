from pathlib import Path

ROOT = Path(__file__).parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def all_kubernetes_yaml() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "k8s").rglob("*.yaml"))


def test_dockerfile_uses_lockfile_and_fixed_non_root_user() -> None:
    dockerfile = read("Dockerfile")

    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv sync --frozen --no-install-project --no-dev" in dockerfile
    assert "ghcr.io/astral-sh/uv" not in dockerfile
    assert "pip install --no-cache-dir" in dockerfile
    assert "ARG UV_VERSION=0.11.21" in dockerfile
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
    assert "https://mirrors.tuna.tsinghua.edu.cn" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "/data/uploads" in dockerfile
    assert "/data/data-files" in dockerfile
    assert "data/ /app/data/" in dockerfile


def test_jenkins_installs_uv_before_host_verification() -> None:
    jenkinsfile = read("Jenkinsfile")

    install_position = jenkinsfile.index("pip install uv")
    sync_position = jenkinsfile.index("uv sync --frozen --group dev")
    assert install_position < sync_position
    assert 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"' in jenkinsfile
    assert "docker build --target test" not in jenkinsfile


def test_pre_push_hook_runs_all_jenkins_verification_commands() -> None:
    hook = read(".githooks/pre-push")
    attributes = read(".gitattributes")
    readme = read("README.md")

    commands = [
        "uv sync --frozen --group dev",
        "uv run pytest -q",
        "uv run ruff check .",
        "uv run ruff format --check .",
    ]
    positions = [hook.index(command) for command in commands]

    assert hook.startswith("#!/bin/sh")
    assert positions == sorted(positions)
    assert ".githooks/* text eol=lf" in attributes
    assert "git config core.hooksPath .githooks" in readme


def test_jenkins_uses_installed_credentials_binding_steps() -> None:
    jenkinsfile = read("Jenkinsfile")

    assert "docker.withRegistry" not in jenkinsfile
    assert "withKubeConfig" not in jenkinsfile
    assert "withCredentials([usernamePassword(" in jenkinsfile
    assert "withCredentials([file(" in jenkinsfile
    assert "172.16.10.17:7747" in jenkinsfile
    assert "yntrust-test/code-sandbox" in jenkinsfile
    assert "yntrust-dev/code-sandbox" in jenkinsfile


def test_jenkins_validates_one_rendered_image_and_prints_rollout_diagnostics() -> None:
    jenkinsfile = read("Jenkinsfile")

    render_position = jenkinsfile.index('kube kustomize "$OVERLAY_PATH"')
    source_check_position = jenkinsfile.index(
        'SOURCE_MATCH_COUNT=$(grep -Fc "image: $SOURCE_IMAGE" "$RENDERED_MANIFEST" || true)'
    )
    replace_position = jenkinsfile.index(
        'sed -i "s|image: $SOURCE_IMAGE|image: $IMAGE_REF|" "$RENDERED_MANIFEST"'
    )
    verify_position = jenkinsfile.index(
        'IMAGE_MATCH_COUNT=$(grep -Fc "image: $IMAGE_REF" "$RENDERED_MANIFEST" || true)'
    )
    cleanup_position = jenkinsfile.index(
        "delete horizontalpodautoscaler/code-sandbox poddisruptionbudget/code-sandbox"
    )
    apply_position = jenkinsfile.index('apply -f "$RENDERED_MANIFEST"')
    assert (
        render_position
        < source_check_position
        < replace_position
        < verify_position
        < cleanup_position
        < apply_position
    )
    assert "render_kustomization.py" not in jenkinsfile
    assert "apply -k k8s/overlays/prod" not in jenkinsfile
    assert "kube set image" not in jenkinsfile
    assert "rollout status deployment/code-sandbox-deployment --timeout=300s" in jenkinsfile
    assert "describe deployment code-sandbox-deployment" in jenkinsfile
    assert "describe pvc" not in jenkinsfile
    assert "describe pod" in jenkinsfile
    assert 'logs "$pod" --all-containers' in jenkinsfile


def test_deployment_has_container_and_pod_security_controls() -> None:
    manifests = all_kubernetes_yaml()

    assert "runAsUser: 10001" in manifests
    assert "runAsNonRoot: true" in manifests
    assert "readOnlyRootFilesystem: true" in manifests
    assert "allowPrivilegeEscalation: false" in manifests
    assert "drop:" in manifests
    assert "- ALL" in manifests
    assert "type: RuntimeDefault" in manifests


def test_deployment_has_resources_probes_and_writable_mounts() -> None:
    deployment = read("k8s/base/deployment.yaml")
    configmap = read("k8s/base/configmap.yaml")

    assert "requests:" in deployment
    assert "limits:" in deployment
    assert "cpu:" in deployment
    assert "ephemeral-storage:" in deployment
    assert "livenessProbe:" in deployment
    assert "readinessProbe:" in deployment
    assert "mountPath: /data/uploads" in deployment
    assert "mountPath: /data/data-files" in deployment
    assert "mountPath: /app/data" not in deployment
    assert "mountPath: /tmp" in deployment
    assert "persistentVolumeClaim:" not in deployment
    assert deployment.count("emptyDir:") == 3
    assert deployment.count("sizeLimit: 512Mi") == 2
    assert "optional: true" in deployment
    assert 'SANDBOX_RETENTION_DAYS: "30"' in configmap
    assert "SANDBOX_DATA_UPLOAD_DIR: /data/data-files" in configmap
    assert 'SANDBOX_MAX_DATA_FILE_BYTES: "104857600"' in configmap
    assert 'SANDBOX_MAX_OUTPUT_BYTES: "10485760"' in configmap
    assert 'SANDBOX_MAX_TIMEOUT_SECONDS: "240"' in configmap


def test_kubernetes_uses_one_ephemeral_pod_without_scaling_resources() -> None:
    manifests = all_kubernetes_yaml()
    deployment = read("k8s/base/deployment.yaml")
    kustomization = read("k8s/base/kustomization.yaml")

    assert "replicas: 1" in deployment
    assert "type: Recreate" in deployment
    assert "pvc.yaml" not in kustomization
    assert "hpa.yaml" not in kustomization
    assert "pdb.yaml" not in kustomization
    assert "kind: PersistentVolumeClaim" not in manifests
    assert "kind: HorizontalPodAutoscaler" not in manifests
    assert "kind: PodDisruptionBudget" not in manifests
    assert "kind: Service" in manifests
    assert "nodePort: 32004" in manifests
    assert "nodePort: 32014" in manifests


def test_kubernetes_service_exposes_environment_node_ports() -> None:
    dev_patch = read("k8s/overlays/dev/service-patch.yaml")
    test_patch = read("k8s/overlays/test/service-patch.yaml")

    assert "nodePort: 32004" in dev_patch
    assert "nodePort: 32014" in test_patch


def test_kubernetes_exposes_only_environment_nodeports() -> None:
    manifests = all_kubernetes_yaml()
    base_kustomization = read("k8s/base/kustomization.yaml")
    readme = read("README.md")

    assert "ingress.yaml" not in base_kustomization
    assert "kind: Ingress" not in manifests
    assert "Nginx Ingress Controller" not in readme
    assert "k8s/base/ingress.yaml" not in readme


def test_network_policy_denies_egress_by_default() -> None:
    policy = read("k8s/base/network-policy.yaml")

    assert "kind: NetworkPolicy" in policy
    assert "policyTypes:\n    - Egress" in policy
    assert "egress:" not in policy

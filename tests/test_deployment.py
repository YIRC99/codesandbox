import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def read_stage_shell(stage_name: str) -> str:
    jenkinsfile = read("Jenkinsfile")
    stage_start = jenkinsfile.index(f"stage('{stage_name}')")
    next_stage = jenkinsfile.find("stage('", stage_start + 1)
    stage = jenkinsfile[stage_start : next_stage if next_stage >= 0 else None]
    shell_start = stage.index("'''") + 3
    return stage[shell_start : stage.index("'''", shell_start)]


def install_shell_stub(directory: Path, name: str, body: str) -> None:
    executable = directory / name
    executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)


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
    assert "REGISTRY_HOST          = '172.16.10.30:7747'" in jenkinsfile
    assert "DOCKER_CREDENTIAL_ID   = 'f41569d7-7caf-439c-bb79-43416888c346'" in jenkinsfile
    assert "NON_PROD_K8S_CREDENTIAL_ID = 'c397a5b5-bc78-4749-b03c-823eddb4aece'" in jenkinsfile
    assert "PROD_K8S_CREDENTIAL_ID = 'lingneng-multi-agent-kubeconfig-prod'" in jenkinsfile


def test_jenkins_maps_main_to_prod_and_rejects_job_branch_mismatches() -> None:
    jenkinsfile = read("Jenkinsfile")

    assert "properties([disableConcurrentBuilds()])" in jenkinsfile
    assert "def resolveTargetEnvironment()" in jenkinsfile
    assert "env.CHECKED_OUT_BRANCH = (scmVars.GIT_BRANCH ?: '').toString()" in jenkinsfile
    assert "jobName.endsWith('-prod')" in jenkinsfile
    assert "branch == 'main'" in jenkinsfile
    assert "branchTarget = 'prod'" in jenkinsfile
    assert "def expectedJobBranch = [dev: 'dev', test: 'test', prod: 'main'][jobTarget]" in (
        jenkinsfile
    )
    assert "if (jobTarget && branch != expectedJobBranch)" in jenkinsfile
    assert "Jenkins job/branch target mismatch" in jenkinsfile
    assert "env.REGISTRY_PROJECT = 'yntrust-prod'" in jenkinsfile
    assert "env.NAMESPACE = 'yntrust-prod'" in jenkinsfile
    assert 'env.OVERLAY_PATH = "k8s/overlays/${env.TARGET_ENV}"' in jenkinsfile


def test_jenkins_verifies_the_approved_production_cluster() -> None:
    jenkinsfile = read("Jenkinsfile")

    assert "PROD_CLUSTER_ID       = 'c-ndk6g'" in jenkinsfile
    assert "PROD_KUBE_SYSTEM_UID   = 'd209eee5-0851-405d-a37b-9cae4dd506a7'" in jenkinsfile
    assert '*/k8s/clusters/"$PROD_CLUSTER_ID")' in jenkinsfile
    assert "get namespace kube-system -o jsonpath='{.metadata.uid}'" in jenkinsfile
    assert "Kubernetes credential points to an unapproved production cluster" in jenkinsfile


def test_jenkins_shell_blocks_have_only_valid_groovy_escape_sequences() -> None:
    jenkinsfile = read("Jenkinsfile")
    invalid_escapes: list[tuple[int, str]] = []

    valid_escaped_characters = set("btnfr'\"\\$u\r\n")
    for block_match in re.finditer(r"'''(.*?)'''", jenkinsfile, flags=re.DOTALL):
        block = block_match.group(1)
        for escape_match in re.finditer(r"(\\+)(.)", block, flags=re.DOTALL):
            backslashes, escaped_character = escape_match.groups()
            if len(backslashes) % 2 == 0 or escaped_character in valid_escaped_characters:
                continue
            line = (
                jenkinsfile.count(
                    "\n",
                    0,
                    block_match.start(1) + escape_match.start(),
                )
                + 1
            )
            invalid_escapes.append((line, backslashes + escaped_character))

    assert invalid_escapes == []


def test_prod_preflight_is_independent_of_other_project_resources(tmp_path: Path) -> None:
    command_directory = tmp_path / "bin"
    command_directory.mkdir()
    kubectl_log = tmp_path / "kubectl.log"
    source_kubeconfig = tmp_path / "source-kubeconfig"
    source_kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    install_shell_stub(
        command_directory,
        "kubectl",
        r"""
printf '%s\n' "$*" >> "$KUBECTL_LOG"
case "$*" in
  *"yunnan-agent"*)
    printf 'cross-project Kubernetes access is forbidden in this test\n' >&2
    exit 86
    ;;
  *"jsonpath={.contexts[0].context.cluster}"*) printf 'prod-cluster' ;;
  *"jsonpath={.clusters[0].cluster.server}"*) printf 'https://prod.example:6443' ;;
  *"get namespace kube-system"*) printf '%s' "$PROD_KUBE_SYSTEM_UID" ;;
  *"auth can-i"*) printf 'yes\n' ;;
  *"get secret code-sandbox-secret -o go-template="*) printf 'encoded-api-key' ;;
  *) exit 0 ;;
esac
""".strip(),
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{command_directory}:{os.environ['PATH']}",
            "KUBECTL_LOG": str(kubectl_log),
            "SOURCE_KUBECONFIG": str(source_kubeconfig),
            "DEPLOY_KUBECONFIG": str(tmp_path / "deploy-kubeconfig"),
            "WORKSPACE": str(tmp_path / "workspace"),
            "TARGET_ENV": "prod",
            "PROD_CLUSTER_ID": "c-ndk6g",
            "PROD_KUBE_SYSTEM_UID": "d209eee5-0851-405d-a37b-9cae4dd506a7",
            "NAMESPACE": "yntrust-prod",
            "SANDBOX_SECRET_NAME": "code-sandbox-secret",
            "AI_DEPLOYMENT": "yunnan-agent",
            "AI_CONFIG_NAME": "yntrust-yunnan-agent-prod",
            "AI_SERVICE": "yunnan-agent-api",
            "AI_SERVICE_PORT": "32001",
            "SANDBOX_SECRET_CHECKSUM_ANNOTATION": "code-sandbox-secret-sha256",
            "AI_CONFIG_CHECKSUM_ANNOTATION": "code-sandbox-config-sha256",
            "EXPECTED_PROD_SANDBOX_URL": "http://code-sandbox.yntrust-prod.svc:32004",
        }
    )
    result = subprocess.run(
        ["/bin/sh"],
        input=read_stage_shell("Prepare Kubernetes Access"),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = kubectl_log.read_text(encoding="utf-8")
    assert "get secret code-sandbox-secret" in calls
    assert "yunnan-agent" not in calls


def test_jenkins_requires_only_the_production_sandbox_secret() -> None:
    jenkinsfile = read("Jenkinsfile")

    assert "require_permission get secrets" in jenkinsfile
    assert 'get secret "$SANDBOX_SECRET_NAME"' in jenkinsfile
    assert "Production sandbox Secret has no api-key" in jenkinsfile
    assert "base64 --decode" not in jenkinsfile


def test_prod_requires_the_sandbox_secret_and_non_prod_keeps_it_optional() -> None:
    jenkinsfile = read("Jenkinsfile")
    base_deployment = read("k8s/base/deployment.yaml")
    prod_patch = read("k8s/overlays/prod/deployment-patch.yaml")
    prod_kustomization = read("k8s/overlays/prod/kustomization.yaml")

    assert "optional: true" in base_deployment
    assert "name: SANDBOX_API_KEY" in prod_patch
    assert "name: code-sandbox-secret" in prod_patch
    assert "key: api-key" in prod_patch
    assert "optional: false" in prod_patch
    assert "deployment-patch.yaml" in prod_kustomization
    assert 'PROD_REQUIRED_SECRET_COUNT=$(grep -Fc "optional: false"' in jenkinsfile
    assert 'PROD_OPTIONAL_SECRET_COUNT=$(grep -Fc "optional: true"' in jenkinsfile


def test_prod_rollback_covers_every_mutable_kustomize_resource() -> None:
    jenkinsfile = read("Jenkinsfile")

    for resource in (
        'deployment "$DEPLOYMENT"',
        'configmap "$CONFIG_NAME"',
        'service "$SERVICE"',
        'serviceaccount "$SERVICE_ACCOUNT"',
        'networkpolicy "$NETWORK_POLICY"',
    ):
        assert f"snapshot_resource {resource}" in jenkinsfile

    assert 'restore_resource "$config_existed" configmap "$CONFIG_NAME"' in jenkinsfile
    assert 'restore_resource "$deployment_existed" deployment "$DEPLOYMENT"' in jenkinsfile
    assert 'restore_resource "$service_existed" service "$SERVICE"' in jenkinsfile
    assert (
        'restore_resource "$service_account_existed" serviceaccount "$SERVICE_ACCOUNT"'
        in jenkinsfile
    )
    assert "current_revision=" not in jenkinsfile
    assert "--ignore-not-found -o name" in jenkinsfile
    assert (
        'restore_resource "$network_policy_existed" networkpolicy "$NETWORK_POLICY"' in jenkinsfile
    )


def test_jenkins_uses_internal_health_check_and_delayed_prod_latest() -> None:
    jenkinsfile = read("Jenkinsfile")

    deploy_position = jenkinsfile.index("stage('Deploy')")
    publish_position = jenkinsfile.index("stage('Publish Latest Image')")
    assert deploy_position < publish_position
    assert 'sandbox_health_proxy_path="/api/v1/namespaces/${NAMESPACE}/services/' in (jenkinsfile)
    assert 'http:${SERVICE}:${CONTAINER_PORT}/proxy/health"' in jenkinsfile
    assert 'kubectl --kubeconfig="${DEPLOY_KUBECONFIG}" get --raw "$proxy_path"' in (jenkinsfile)
    assert 'if [ "$TARGET_ENV" != "prod" ]; then' in jenkinsfile
    assert 'docker push "$LATEST_REF"' in jenkinsfile[publish_position:]
    assert 'def imageTag = "prod-${env.GIT_COMMIT_FULL}"' in jenkinsfile


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
    assert 'rollout status deployment/"$DEPLOYMENT" --timeout=300s' in jenkinsfile
    assert 'describe deployment "$DEPLOYMENT"' in jenkinsfile
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
    base_service = read("k8s/base/service.yaml")
    dev_patch = read("k8s/overlays/dev/service-patch.yaml")
    test_patch = read("k8s/overlays/test/service-patch.yaml")
    prod_kustomization = read("k8s/overlays/prod/kustomization.yaml")

    assert "type: ClusterIP" in base_service
    assert "nodePort:" not in base_service
    assert "type: NodePort" in dev_patch
    assert "nodePort: 32004" in dev_patch
    assert "type: NodePort" in test_patch
    assert "nodePort: 32014" in test_patch
    assert "namespace: yntrust-prod" in prod_kustomization
    assert "namespace.yaml" not in prod_kustomization
    assert "172.16.10.30:7747/yntrust-prod/code-sandbox" in prod_kustomization


def test_all_kubernetes_overlays_use_the_current_harbor() -> None:
    for environment in ("dev", "test", "prod"):
        kustomization = read(f"k8s/overlays/{environment}/kustomization.yaml")
        assert f"172.16.10.30:7747/yntrust-{environment}/code-sandbox" in kustomization


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

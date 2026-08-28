properties([disableConcurrentBuilds()])

def resolveTargetEnvironment() {
    def jobName = env.JOB_NAME ?: ''
    def branch = (env.CHECKED_OUT_BRANCH ?: env.BRANCH_NAME ?: env.GIT_BRANCH ?: '')
        .replaceAll('^refs/remotes/origin/', '')
        .replaceAll('^refs/heads/', '')
        .replaceAll('^origin/', '')

    def jobTarget = ''
    if (jobName.endsWith('-prod')) {
        jobTarget = 'prod'
    } else if (jobName.endsWith('-test')) {
        jobTarget = 'test'
    } else if (jobName.endsWith('-dev')) {
        jobTarget = 'dev'
    }

    def branchTarget = ''
    if (branch in ['dev', 'test']) {
        branchTarget = branch
    } else if (branch == 'main') {
        branchTarget = 'prod'
    }
    def expectedJobBranch = [dev: 'dev', test: 'test', prod: 'main'][jobTarget]
    if (jobTarget && branch != expectedJobBranch) {
        error(
            "Jenkins job/branch target mismatch: " +
            "job=${jobName}, branch=${branch}, expectedBranch=${expectedJobBranch}"
        )
    }

    def targetEnvironment = jobTarget ?: branchTarget
    if (!targetEnvironment) {
        error(
            "Unable to resolve deployment target from Jenkins job/branch: " +
            "job=${jobName}, branch=${branch}"
        )
    }
    return targetEnvironment
}

pipeline {
    agent any

    environment {
        DOCKER_CREDENTIAL_ID   = 'f41569d7-7caf-439c-bb79-43416888c346'
        NON_PROD_K8S_CREDENTIAL_ID = 'c397a5b5-bc78-4749-b03c-823eddb4aece'
        PROD_K8S_CREDENTIAL_ID = 'lingneng-multi-agent-kubeconfig-prod'
        PROD_CLUSTER_ID       = 'c-ndk6g'
        PROD_KUBE_SYSTEM_UID   = 'd209eee5-0851-405d-a37b-9cae4dd506a7'

        REGISTRY_HOST          = '172.16.10.30:7747'
        SERVICE                = 'code-sandbox'
        DEPLOYMENT             = 'code-sandbox-deployment'
        CONFIG_NAME            = 'code-sandbox-config'
        SERVICE_ACCOUNT        = 'code-sandbox'
        NETWORK_POLICY         = 'code-sandbox-deny-egress'
        SANDBOX_SECRET_NAME    = 'code-sandbox-secret'
        CONTAINER_PORT         = '32004'
        DOCKER_BUILDKIT        = '1'
        BUILD_TIMESTAMP        = "${currentBuild.startTimeInMillis}"
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    def scmVars = checkout scm
                    env.CHECKED_OUT_BRANCH = (scmVars.GIT_BRANCH ?: '').toString()
                    env.GIT_COMMIT_FULL = sh(
                        script: 'git rev-parse HEAD',
                        returnStdout: true
                    ).trim()
                    echo "Checked out commit: ${env.GIT_COMMIT_FULL}"
                }
            }
        }

        stage('Verify') {
            steps {
                sh '''
                    set -eu
                    if ! command -v uv >/dev/null 2>&1; then
                        pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple --user \
                            || pip3 install uv -i https://pypi.tuna.tsinghua.edu.cn/simple --user \
                            || curl -LsSf https://astral.sh/uv/install.sh | sh
                    fi
                    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
                    uv --version
                    uv sync --frozen --group dev
                    uv run pytest -q
                    uv run ruff check .
                    uv run ruff format --check .
                '''
            }
        }

        stage('Setup Environment') {
            steps {
                script {
                    env.TARGET_ENV = resolveTargetEnvironment()

                    if (env.TARGET_ENV == 'prod') {
                        env.REGISTRY_PROJECT = 'yntrust-prod'
                        env.NAMESPACE = 'yntrust-prod'
                    } else if (env.TARGET_ENV == 'test') {
                        env.REGISTRY_PROJECT = 'yntrust-test'
                        env.NAMESPACE = 'yntrust-test'
                        env.NODE_PORT = '32014'
                    } else if (env.TARGET_ENV == 'dev') {
                        env.REGISTRY_PROJECT = 'yntrust-dev'
                        env.NAMESPACE = 'yntrust-dev'
                        env.NODE_PORT = '32004'
                    } else {
                        error("Unsupported deployment target: ${env.TARGET_ENV}")
                    }

                    if (env.TARGET_ENV == 'prod') {
                        env.K8S_CREDENTIAL_ID = env.PROD_K8S_CREDENTIAL_ID
                    } else {
                        env.K8S_CREDENTIAL_ID = env.NON_PROD_K8S_CREDENTIAL_ID
                        env.HEALTH_URL = "http://172.16.10.15:${env.NODE_PORT}/health"
                    }

                    def expectedResourcePrefix = "yntrust-${env.TARGET_ENV}"
                    if (
                        env.REGISTRY_PROJECT != expectedResourcePrefix ||
                        env.NAMESPACE != expectedResourcePrefix
                    ) {
                        error(
                            "Deployment target configuration mismatch: " +
                            "target=${env.TARGET_ENV}, registry=${env.REGISTRY_PROJECT}, " +
                            "namespace=${env.NAMESPACE}"
                        )
                    }

                    if (env.TARGET_ENV == 'prod') {
                        def imageTag = "prod-${env.GIT_COMMIT_FULL}"
                        env.IMAGE_REF = "${env.REGISTRY_HOST}/${env.REGISTRY_PROJECT}/${env.SERVICE}:${imageTag}"
                        env.LATEST_REF = "${env.REGISTRY_HOST}/${env.REGISTRY_PROJECT}/${env.SERVICE}:prod-latest"
                    } else {
                        env.IMAGE_REF = "${env.REGISTRY_HOST}/${env.REGISTRY_PROJECT}/${env.SERVICE}:${env.BUILD_NUMBER}"
                        env.LATEST_REF = "${env.REGISTRY_HOST}/${env.REGISTRY_PROJECT}/${env.SERVICE}:latest"
                    }
                    env.OVERLAY_PATH = "k8s/overlays/${env.TARGET_ENV}"
                    env.DEPLOY_KUBECONFIG = "${env.WORKSPACE}@tmp/code-sandbox-kubeconfig-${env.BUILD_NUMBER}"

                    echo "Target environment: ${env.TARGET_ENV}"
                    echo "Kubernetes namespace: ${env.NAMESPACE}"
                    echo "Image: ${env.IMAGE_REF}"
                }
            }
        }

        stage('Prepare Kubernetes Access') {
            steps {
                withCredentials([file(credentialsId: env.K8S_CREDENTIAL_ID, variable: 'SOURCE_KUBECONFIG')]) {
                    sh '''
                        set -eu
                        set +x
                        mkdir -p "$WORKSPACE@tmp"
                        install -m 600 "$SOURCE_KUBECONFIG" "$DEPLOY_KUBECONFIG"

                        if [ "$TARGET_ENV" = "prod" ]; then
                            cluster_name="$(kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                config view --minify -o jsonpath='{.contexts[0].context.cluster}')"
                            current_server="$(kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
                            case "$current_server" in
                                */k8s/clusters/"$PROD_CLUSTER_ID")
                                    ;;
                                */k8s/clusters/*)
                                    rancher_base="${current_server%/k8s/clusters/*}"
                                    target_server="${rancher_base}/k8s/clusters/${PROD_CLUSTER_ID}"
                                    kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                        config set-cluster "$cluster_name" --server="$target_server" \
                                        >/dev/null
                                    ;;
                                *)
                                    direct_cluster_uid="$(
                                        kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                            get namespace kube-system \
                                            -o jsonpath='{.metadata.uid}' 2>/dev/null \
                                            || true
                                    )"
                                    if [ "$direct_cluster_uid" = "$PROD_KUBE_SYSTEM_UID" ]; then
                                        printf 'Using verified direct production Kubernetes API.\n'
                                    else
                                        target_server="${current_server%/}/k8s/clusters/${PROD_CLUSTER_ID}"
                                        kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                            config set-cluster "$cluster_name" \
                                            --server="$target_server" >/dev/null
                                    fi
                                    ;;
                            esac

                            selected_server="$(kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
                            case "$selected_server" in
                                */k8s/clusters/"$PROD_CLUSTER_ID") ;;
                                */k8s/clusters/*)
                                    printf 'Kubernetes credential did not select the production cluster.\n' >&2
                                    exit 1
                                    ;;
                                *) ;;
                            esac

                            kube_system_uid="$(kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                get namespace kube-system -o jsonpath='{.metadata.uid}')"
                            if [ "$kube_system_uid" != "$PROD_KUBE_SYSTEM_UID" ]; then
                                printf 'Kubernetes credential points to an unapproved production cluster.\n' >&2
                                exit 1
                            fi
                        fi

                        kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                            get namespace "$NAMESPACE" >/dev/null

                        require_permission() {
                            verb="$1"
                            resource="$2"
                            allowed="$(kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                auth can-i "$verb" "$resource" -n "$NAMESPACE")"
                            if [ "$allowed" != "yes" ]; then
                                printf 'Missing Kubernetes permission: %s %s in %s.\n' \
                                    "$verb" "$resource" "$NAMESPACE" >&2
                                return 1
                            fi
                        }

                        for resource in \
                            configmaps \
                            deployments.apps \
                            services \
                            serviceaccounts \
                            networkpolicies.networking.k8s.io; do
                            require_permission get "$resource"
                            require_permission create "$resource"
                            require_permission patch "$resource"
                        done
                        require_permission watch deployments.apps

                        if [ "$TARGET_ENV" = "prod" ]; then
                            require_permission get secrets
                            require_permission get services/proxy
                            require_permission delete deployments.apps
                            for resource in \
                                configmaps \
                                services \
                                serviceaccounts \
                                networkpolicies.networking.k8s.io; do
                                require_permission delete "$resource"
                            done
                        else
                            require_permission delete horizontalpodautoscalers.autoscaling
                            require_permission delete poddisruptionbudgets.policy
                        fi

                        if [ "$TARGET_ENV" = "prod" ]; then
                            kubectl --kubeconfig="$DEPLOY_KUBECONFIG" -n "$NAMESPACE" \
                                get secret "$SANDBOX_SECRET_NAME" >/dev/null
                            sandbox_key_b64="$(kubectl --kubeconfig="$DEPLOY_KUBECONFIG" \
                                -n "$NAMESPACE" get secret "$SANDBOX_SECRET_NAME" \
                                -o go-template='{{index .data "api-key"}}')"
                            if [ -z "$sandbox_key_b64" ]; then
                                printf 'Production sandbox Secret has no api-key.\n' >&2
                                exit 1
                            fi
                            unset sandbox_key_b64
                        fi
                    '''
                }
            }
        }

        stage('Build Docker Image') {
            steps {
                sh '''
                    set -eu
                    docker build -t "$IMAGE_REF" .
                    docker tag "$IMAGE_REF" "$LATEST_REF"
                '''
            }
        }

        stage('Push Image') {
            steps {
                withCredentials([usernamePassword(credentialsId: env.DOCKER_CREDENTIAL_ID, usernameVariable: 'HARBOR_USER', passwordVariable: 'HARBOR_PASS')]) {
                    sh '''
                        set -eu
                        set +x
                        echo "$HARBOR_PASS" | docker login "$REGISTRY_HOST" \
                            -u "$HARBOR_USER" --password-stdin
                        docker push "$IMAGE_REF"
                        if [ "$TARGET_ENV" != "prod" ]; then
                            docker push "$LATEST_REF"
                        fi
                    '''
                }
            }
        }

        stage('Deploy') {
            steps {
                sh '''
                    set -eu
                    set +x
                    kube() {
                        kubectl --kubeconfig="$DEPLOY_KUBECONFIG" "$@"
                    }

                    RENDERED_MANIFEST="$(mktemp)"
                    ROLLBACK_DIR="$(mktemp -d)"
                    deployment_existed=false
                    service_existed=false
                    config_existed=false
                    service_account_existed=false
                    network_policy_existed=false
                    previous_replicas=''
                    K8S_MUTATED=false

                    rollback_and_cleanup() {
                        status="$?"
                        trap - 0
                        if [ "$status" -ne 0 ] && \
                           [ "$TARGET_ENV" = "prod" ] && \
                           [ "$K8S_MUTATED" = "true" ]; then
                            set +e
                            printf 'Production deployment failed; restoring the previous resources.\n' >&2

                            restore_resource() {
                                existed="$1"
                                kind="$2"
                                name="$3"
                                snapshot="$ROLLBACK_DIR/${kind}-${name}.json"
                                if [ "$existed" = "true" ]; then
                                    kube apply -f "$snapshot"
                                else
                                    kube -n "$NAMESPACE" delete "$kind" "$name" \
                                        --ignore-not-found=true
                                fi
                            }

                            restore_resource "$config_existed" configmap "$CONFIG_NAME"
                            restore_resource "$service_account_existed" serviceaccount "$SERVICE_ACCOUNT"
                            restore_resource "$network_policy_existed" networkpolicy "$NETWORK_POLICY"
                            restore_resource "$service_existed" service "$SERVICE"
                            restore_resource "$deployment_existed" deployment "$DEPLOYMENT"

                            if [ "$deployment_existed" = "true" ]; then
                                replica_patch="$(printf '{\"spec\":{\"replicas\":%s}}' \
                                    "$previous_replicas")"
                                kube -n "$NAMESPACE" patch deployment "$DEPLOYMENT" \
                                    --type=merge -p "$replica_patch"
                                kube -n "$NAMESPACE" rollout status deployment/"$DEPLOYMENT" \
                                    --timeout=300s
                            fi
                        fi
                        rm -f "$RENDERED_MANIFEST"
                        rm -f "$ROLLBACK_DIR"/*.json
                        rmdir "$ROLLBACK_DIR"
                        exit "$status"
                    }
                    trap rollback_and_cleanup 0

                    SOURCE_IMAGE="$LATEST_REF"
                    kube kustomize "$OVERLAY_PATH" > "$RENDERED_MANIFEST"
                    SOURCE_MATCH_COUNT=$(grep -Fc "image: $SOURCE_IMAGE" "$RENDERED_MANIFEST" || true)
                    if [ "$SOURCE_MATCH_COUNT" -ne 1 ]; then
                        echo "Expected one rendered source image, found $SOURCE_MATCH_COUNT: $SOURCE_IMAGE" >&2
                        grep -n "image:" "$RENDERED_MANIFEST" >&2 || true
                        exit 1
                    fi

                    sed -i "s|image: $SOURCE_IMAGE|image: $IMAGE_REF|" "$RENDERED_MANIFEST"
                    IMAGE_MATCH_COUNT=$(grep -Fc "image: $IMAGE_REF" "$RENDERED_MANIFEST" || true)
                    if [ "$IMAGE_MATCH_COUNT" -ne 1 ]; then
                        echo "Expected one rendered deployment image, found $IMAGE_MATCH_COUNT: $IMAGE_REF" >&2
                        grep -n "image:" "$RENDERED_MANIFEST" >&2 || true
                        exit 1
                    fi

                    if [ "$TARGET_ENV" = "prod" ]; then
                        snapshot_resource() {
                            kind="$1"
                            name="$2"
                            if ! existing_resource="$(kube -n "$NAMESPACE" get "$kind" "$name" \
                                --ignore-not-found -o name)"; then
                                printf 'Failed to inspect production resource: %s/%s.\n' \
                                    "$kind" "$name" >&2
                                return 1
                            fi
                            if [ -n "$existing_resource" ]; then
                                last_applied="$(kube -n "$NAMESPACE" get "$kind" "$name" \
                                    -o jsonpath='{.metadata.annotations.kubectl\\.kubernetes\\.io/last-applied-configuration}')"
                                if [ -z "$last_applied" ]; then
                                    printf 'Existing production resource is not managed by kubectl apply: %s/%s.\n' \
                                        "$kind" "$name" >&2
                                    return 1
                                fi
                                printf '%s' "$last_applied" \
                                    > "$ROLLBACK_DIR/${kind}-${name}.json"
                                printf 'true'
                            else
                                printf 'false'
                            fi
                            unset existing_resource last_applied
                        }

                        deployment_existed="$(snapshot_resource deployment "$DEPLOYMENT")"
                        config_existed="$(snapshot_resource configmap "$CONFIG_NAME")"
                        service_existed="$(snapshot_resource service "$SERVICE")"
                        service_account_existed="$(snapshot_resource serviceaccount "$SERVICE_ACCOUNT")"
                        network_policy_existed="$(snapshot_resource networkpolicy "$NETWORK_POLICY")"

                        if grep -Eq '^[[:space:]]*(type: NodePort|nodePort:)' "$RENDERED_MANIFEST"; then
                            printf 'Production manifest must not expose a NodePort.\n' >&2
                            exit 1
                        fi
                        PROD_REQUIRED_SECRET_COUNT=$(grep -Fc "optional: false" \
                            "$RENDERED_MANIFEST" || true)
                        PROD_OPTIONAL_SECRET_COUNT=$(grep -Fc "optional: true" \
                            "$RENDERED_MANIFEST" || true)
                        if [ "$PROD_REQUIRED_SECRET_COUNT" -ne 1 ] || \
                           [ "$PROD_OPTIONAL_SECRET_COUNT" -ne 0 ]; then
                            printf 'Production manifest must require exactly one sandbox Secret reference.\n' >&2
                            exit 1
                        fi
                        if [ "$deployment_existed" = "true" ]; then
                            previous_replicas="$(kube -n "$NAMESPACE" get deployment "$DEPLOYMENT" \
                                -o jsonpath='{.spec.replicas}')"
                            case "$previous_replicas" in
                                ''|*[!0-9]*)
                                    printf 'Existing production Deployment has invalid replicas.\n' >&2
                                    exit 1
                                    ;;
                            esac
                        fi
                        if [ "$service_existed" = "true" ]; then
                            existing_service_type="$(kube -n "$NAMESPACE" get service "$SERVICE" \
                                -o jsonpath='{.spec.type}')"
                            if [ "$existing_service_type" != "ClusterIP" ]; then
                                printf 'Existing production Service must be ClusterIP.\n' >&2
                                exit 1
                            fi
                        fi
                        if [ "$deployment_existed" != "$service_existed" ]; then
                            printf 'Production Deployment and Service must both exist or both be absent.\n' >&2
                            exit 1
                        fi
                        if kube -n "$NAMESPACE" get horizontalpodautoscaler "$SERVICE" >/dev/null 2>&1 || \
                           kube -n "$NAMESPACE" get poddisruptionbudget "$SERVICE" >/dev/null 2>&1; then
                            printf 'Unexpected production scaling resource must be removed before deployment.\n' >&2
                            exit 1
                        fi
                    else
                        kube delete horizontalpodautoscaler/code-sandbox poddisruptionbudget/code-sandbox \
                            --ignore-not-found -n "$NAMESPACE"
                    fi

                    K8S_MUTATED=true
                    kube apply -f "$RENDERED_MANIFEST"
                    kube annotate deployment/"$DEPLOYMENT" \
                        kubernetes.io/change-cause="Jenkins build $BUILD_NUMBER ($GIT_COMMIT_FULL)" \
                        --overwrite -n "$NAMESPACE"

                    if ! kube rollout status deployment/"$DEPLOYMENT" --timeout=300s -n "$NAMESPACE"; then
                        echo "=== Deployment rollout failed: diagnostics ==="
                        kube get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o wide || true
                        kube describe deployment "$DEPLOYMENT" -n "$NAMESPACE" || true
                        kube get pods -l app.kubernetes.io/name=code-sandbox -n "$NAMESPACE" -o wide || true
                        kube get events -n "$NAMESPACE" --sort-by=.lastTimestamp | tail -100 || true

                        pods=$(kube get pods -l app.kubernetes.io/name=code-sandbox \
                            -n "$NAMESPACE" -o jsonpath='{.items[*].metadata.name}')
                        for pod in $pods; do
                            echo "=== Pod: $pod ==="
                            kube describe pod "$pod" -n "$NAMESPACE" || true
                            kube logs "$pod" --all-containers --tail=200 -n "$NAMESPACE" || true
                        done
                        exit 1
                    fi

                    check_url_with_retry() {
                        url="$1"
                        for attempt in $(seq 1 30); do
                            if curl -fsS "$url"; then
                                printf '\n'
                                return 0
                            fi
                            printf 'Waiting for %s (attempt %s/30)\n' "$url" "$attempt" >&2
                            sleep 2
                        done
                        curl -fsS "$url"
                    }

                    check_proxy_with_retry() {
                        proxy_path="$1"
                        for attempt in $(seq 1 30); do
                            if kubectl --kubeconfig="${DEPLOY_KUBECONFIG}" get --raw "$proxy_path"; then
                                printf '\n'
                                return 0
                            fi
                            printf 'Waiting for Kubernetes Service proxy %s (attempt %s/30)\n' \
                                "$proxy_path" "$attempt" >&2
                            sleep 2
                        done
                        kubectl --kubeconfig="${DEPLOY_KUBECONFIG}" get --raw "$proxy_path"
                    }

                    if [ "$TARGET_ENV" = "prod" ]; then
                        sandbox_health_proxy_path="/api/v1/namespaces/${NAMESPACE}/services/http:${SERVICE}:${CONTAINER_PORT}/proxy/health"
                        check_proxy_with_retry "$sandbox_health_proxy_path"
                    else
                        check_url_with_retry "$HEALTH_URL"
                    fi
                    K8S_MUTATED=false
                '''
            }
        }

        stage('Publish Latest Image') {
            steps {
                script {
                    if (env.TARGET_ENV == 'prod') {
                        withCredentials([usernamePassword(credentialsId: env.DOCKER_CREDENTIAL_ID, usernameVariable: 'HARBOR_USER', passwordVariable: 'HARBOR_PASS')]) {
                            sh '''
                                set -eu
                                set +x
                                echo "$HARBOR_PASS" | docker login "$REGISTRY_HOST" \
                                    -u "$HARBOR_USER" --password-stdin
                                docker push "$LATEST_REF"
                            '''
                        }
                    } else {
                        echo "Latest image was published before ${env.TARGET_ENV} deployment."
                    }
                }
            }
        }
    }

    post {
        always {
            sh '''
                if [ -n "${DEPLOY_KUBECONFIG:-}" ]; then
                    rm -f "$DEPLOY_KUBECONFIG" || true
                fi
            '''
            sh 'docker logout "$REGISTRY_HOST" >/dev/null 2>&1 || true'
            sh 'docker image prune -f || true'
        }
        success {
            echo "Code Sandbox ${GIT_COMMIT_FULL} deployed successfully to ${NAMESPACE}."
        }
        failure {
            echo 'Build or deployment failed. Check the failing stage output.'
        }
    }
}

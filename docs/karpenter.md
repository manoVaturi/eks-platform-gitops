# Karpenter on the weatherapp cluster

Step-by-step guide to add [Karpenter](https://karpenter.sh) (node autoscaling) to this GitOps repo.
It follows the repo's conventions: Argo CD app-of-apps, one explicit Application per platform component,
values in `platform/<group>/<name>/values.yaml`, in-house charts in `charts/`, AWS access through **Pod Identity**.

> Facts taken from this repo: EKS cluster `weatherapp`, region `eu-west-1`, VPC `<VPC_ID>`,
> platform components in `kube-system`, Vault data on a static EBS volume pinned to `eu-west-1a`,
> Istio ambient (ztunnel DaemonSet), Falco DaemonSet, VPA in recommendation-only mode, HPA disabled by default.

## What changes

| Today | With Karpenter |
|---|---|
| Fixed node group; pending pods stay pending | Pending pods trigger a new EC2 node within ~1 minute |
| Empty or under-used nodes stay | Karpenter consolidates and removes them |
| One instance type | Karpenter picks the cheapest fitting type (Spot or On-Demand) |

Karpenter does **not** replace the HPA (pods) or the VPA (pod sizing). It adds nodes when pods cannot be scheduled.

## Architecture and ownership

```
AWS (outside this repo - Terraform / your infra layers)     This repo (Argo CD)
------------------------------------------------------      ---------------------------------
Controller IAM role  + Pod Identity association             argocd/platform/system/karpenter.yaml          (chart, wave 0)
Node IAM role        + EKS access entry                     argocd/platform/system/karpenter-resources.yaml (wave 1)
SQS interruption queue + EventBridge rules                  platform/system/karpenter/values.yaml
Subnet / SG discovery tags (karpenter.sh/discovery)         charts/karpenter-resources/  (NodePool + EC2NodeClass)
Small managed node group (runs Karpenter itself)
```

Rule: **Karpenter must not run on nodes it manages.** Keep a small managed node group (2 x `t3.medium`/`m6i.large`) for
Karpenter, CoreDNS, Argo CD and anything else you do not want disrupted.

## Prerequisites

- [ ] EKS cluster `weatherapp` reachable with `kubectl`, authentication mode includes **access entries** (`API` or `API_AND_CONFIG_MAP`)
- [ ] EKS Pod Identity Agent add-on installed (already used by the AWS Load Balancer Controller)
- [ ] `aws`, `kubectl`, `helm`, `terraform` installed
- [ ] Decide the Karpenter version. Check the latest 1.x release and the EKS compatibility matrix at
      <https://karpenter.sh/docs/upgrading/compatibility/>. Below it is written as `<KARPENTER_VERSION>`.

```bash
export CLUSTER_NAME=weatherapp
export AWS_REGION=eu-west-1
export KARPENTER_VERSION=<KARPENTER_VERSION>     # e.g. a 1.x release, no leading "v"
```

## Step 1 - AWS resources (infra repo)

Easiest path: the `karpenter` submodule of `terraform-aws-modules/eks`. It creates everything below in one go.

```hcl
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"

  cluster_name = "weatherapp"

  # Pod Identity (same mechanism as aws-load-balancer-controller in this repo)
  enable_pod_identity             = true
  create_pod_identity_association = true
  namespace                       = "kube-system"
  service_account                 = "karpenter"

  # Node role that Karpenter-launched instances use
  node_iam_role_use_name_prefix = false
  node_iam_role_name            = "KarpenterNodeRole-weatherapp"
  create_access_entry           = true   # lets those nodes join the cluster

  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }
}
```

This gives you:

1. **Controller IAM role** + Pod Identity association for `kube-system/karpenter`
2. **Node IAM role** `KarpenterNodeRole-weatherapp` (+ EKS access entry of type `EC2_LINUX`)
3. **SQS queue + EventBridge rules** (Spot interruption, rebalance, instance state change, scheduled change)

Note the output `queue_name` - you need it in step 3.

### Tag subnets and the node security group

Karpenter finds where to launch nodes by tag. Tag the **private** subnets and the **node security group**:

```bash
aws ec2 create-tags --region $AWS_REGION \
  --resources <subnet-a> <subnet-b> <subnet-c> <node-sg-id> \
  --tags Key=karpenter.sh/discovery,Value=$CLUSTER_NAME
```

(or `tags = { "karpenter.sh/discovery" = "weatherapp" }` in the Terraform for the subnets / SG).

### Make sure the managed node group is protected

The existing node group must keep running the controller. Optionally taint it so only system pods land there:

```hcl
taints = { critical = { key = "CriticalAddonsOnly", value = "true", effect = "NO_SCHEDULE" } }
```

If you do, the Karpenter chart tolerates `CriticalAddonsOnly` by default; Argo CD, CoreDNS, etc. need the same toleration.

### Verify

```bash
aws iam get-role --role-name KarpenterNodeRole-weatherapp --query Role.Arn
aws sqs list-queues --region $AWS_REGION | grep -i weatherapp
aws eks list-pod-identity-associations --cluster-name $CLUSTER_NAME --namespace kube-system
```

## Step 2 - Allow the chart repo in the AppProject

The Karpenter chart is published as an OCI chart. Argo CD references it **without** `oci://`.
Edit `argocd/projects/platform.yaml` and add to `sourceRepos`:

```yaml
    - public.ecr.aws/karpenter
```

`kube-system` is already an allowed destination, and cluster-scoped kinds (CRDs, NodePool, EC2NodeClass) are already whitelisted.

## Step 3 - Karpenter controller Application

Create `argocd/platform/system/karpenter.yaml`. Two sources for the chart: the CRD chart (so CRDs are upgraded on version bumps;
the main chart only installs them once) and the controller chart.

```yaml
# Karpenter: node autoscaler. AWS credentials come from Pod Identity.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: karpenter-crd
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
  annotations:
    argocd.argoproj.io/sync-wave: '-1'
spec:
  project: platform
  source:
    repoURL: public.ecr.aws/karpenter
    chart: karpenter-crd
    targetRevision: <KARPENTER_VERSION>
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - ServerSideApply=true
      - Replace=false
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: karpenter
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
  annotations:
    argocd.argoproj.io/sync-wave: '0'
spec:
  project: platform
  sources:
    - repoURL: public.ecr.aws/karpenter
      chart: karpenter
      targetRevision: <KARPENTER_VERSION>
      helm:
        skipCrds: true            # CRDs come from karpenter-crd
        ignoreMissingValueFiles: true
        valueFiles:
          - $values/platform/system/karpenter/values.yaml
    - repoURL: https://gitlab.manovaturi.com/weatherapp/weatherapp-gitops.git
      targetRevision: main
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - ServerSideApply=true
```

> The repo convention is one Application per file. If you prefer that, split into `karpenter-crd.yaml` and `karpenter.yaml`.
> Update the table at the end of the main `README.md` ("Sync order") when you add them.

Create `platform/system/karpenter/values.yaml`:

```yaml
# Chart karpenter <KARPENTER_VERSION>. AWS credentials come from Pod Identity (no IRSA annotation needed).
serviceAccount:
  name: karpenter            # must match the Pod Identity association

settings:
  clusterName: weatherapp
  interruptionQueue: <SQS_QUEUE_NAME>   # output of step 1
  featureGates:
    spotToSpotConsolidation: true

controller:
  resources:
    requests: { cpu: 100m, memory: 256Mi }
    limits: { memory: 512Mi }

replicas: 2                  # use 1 if the managed node group has a single node (the chart spreads replicas by default)

# Run only on the managed node group, never on Karpenter-provisioned nodes.
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: karpenter.sh/nodepool
              operator: DoesNotExist
```

## Step 4 - NodePool and EC2NodeClass chart

Create a small in-house chart so the objects are versioned and linted like the others.

`charts/karpenter-resources/Chart.yaml`

```yaml
apiVersion: v2
name: karpenter-resources
description: Karpenter EC2NodeClass and NodePools for the weatherapp cluster
version: 0.1.0
```

`charts/karpenter-resources/values.yaml`

```yaml
clusterName: weatherapp
nodeRole: KarpenterNodeRole-weatherapp

nodePools:
  # Stateless workloads: weatherapp, monitoring, redis. Spot first, On-Demand fallback.
  - name: default
    capacityTypes: [spot, on-demand]
    instanceCategories: [c, m, r]
    instanceGenerationGt: '5'
    architectures: [amd64]
    cpuLimit: '16'                    # hard cap on total vCPU - the cost safety net
    consolidateAfter: 1m
  # Stateful workloads: postgres, mongo, vault. On-Demand only, and never consolidated away.
  - name: stateful
    capacityTypes: [on-demand]
    instanceCategories: [m, r]
    instanceGenerationGt: '5'
    architectures: [amd64]
    cpuLimit: '8'
    consolidateAfter: 1h
    zones: [eu-west-1a]               # Vault's EBS volume lives in eu-west-1a
    taint: true                       # pods must tolerate workload=stateful
```

`charts/karpenter-resources/templates/ec2nodeclass.yaml`

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  role: {{ .Values.nodeRole }}
  amiSelectorTerms:
    - alias: al2023@latest            # pin to a version (al2023@v2026....) for production
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: {{ .Values.clusterName }}
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: {{ .Values.clusterName }}
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 30Gi
        volumeType: gp3
        encrypted: true
  metadataOptions:
    httpTokens: required
    httpPutResponseHopLimit: 1        # pods use Pod Identity, not instance metadata
  tags:
    karpenter.sh/discovery: {{ .Values.clusterName }}
```

`charts/karpenter-resources/templates/nodepools.yaml`

```yaml
{{- range .Values.nodePools }}
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: {{ .name }}
spec:
  template:
    metadata:
      labels:
        workload: {{ .name }}
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      {{- if .taint }}
      taints:
        - key: workload
          value: {{ .name }}
          effect: NoSchedule
      {{- end }}
      requirements:
        - { key: karpenter.sh/capacity-type, operator: In, values: {{ .capacityTypes | toJson }} }
        - { key: karpenter.k8s.aws/instance-category, operator: In, values: {{ .instanceCategories | toJson }} }
        - { key: karpenter.k8s.aws/instance-generation, operator: Gt, values: [{{ .instanceGenerationGt | quote }}] }
        - { key: kubernetes.io/arch, operator: In, values: {{ .architectures | toJson }} }
        {{- if .zones }}
        - { key: topology.kubernetes.io/zone, operator: In, values: {{ .zones | toJson }} }
        {{- end }}
      expireAfter: 720h               # replace nodes every 30 days (fresh AMI)
  limits:
    cpu: {{ .cpuLimit | quote }}
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: {{ .consolidateAfter }}
    budgets:
      - nodes: '10%'
{{- end }}
```

The Application, `argocd/platform/system/karpenter-resources.yaml`:

```yaml
# Karpenter EC2NodeClass and NodePools.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: karpenter-resources
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
  annotations:
    argocd.argoproj.io/sync-wave: '1'      # after the controller
spec:
  project: platform
  source:
    repoURL: https://gitlab.manovaturi.com/weatherapp/weatherapp-gitops.git
    targetRevision: main
    path: charts/karpenter-resources
  destination:
    server: https://kubernetes.default.svc
    namespace: kube-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - SkipDryRunOnMissingResource=true   # CRDs may not exist yet at the first sync
```

## Step 5 - Make the stateful workloads land on the right nodes

Because `stateful` is tainted and pinned to `eu-west-1a`, Vault's static PV (zone `eu-west-1a`) can always be attached.
Add to the values of `postgres`, `mongo` and Vault (`platform/vault/vault/values.yaml`):

```yaml
nodeSelector:
  workload: stateful
tolerations:
  - { key: workload, value: stateful, operator: Equal, effect: NoSchedule }
```

(`charts/database` needs `nodeSelector` / `tolerations` passed through its `workload.yaml` template - add them to
the chart's `values.yaml` first.) Also protect running DB pods from voluntary disruption:

```yaml
podAnnotations:
  karpenter.sh/do-not-disrupt: 'true'
```

Stateless apps (`weatherapp`, `redis`, monitoring) need nothing: they go to the `default` pool.

Two settings worth adding to `charts/weatherapp` so consolidation is safe:

- `PodDisruptionBudget` (or `replicaCount: 2`) - Karpenter respects PDBs when it drains
- `topologySpreadConstraints` across `topology.kubernetes.io/zone`

DaemonSets (ztunnel, istio-cni, Falco, Alloy) are scheduled automatically on new nodes. Make sure their requests are set,
because Karpenter counts DaemonSet overhead when it sizes a node.

## Step 6 - Validate and ship

```bash
scripts/validate.sh        # yamllint + helm lint + kubeconform (CRDs are ignored when schemas are missing)
git checkout -b feat/karpenter
git add argocd platform charts docs
git commit -m "Add Karpenter"
git push -u origin feat/karpenter   # open the merge request, CI runs validate.sh
```

After merge, Argo CD syncs in this order: `karpenter-crd` (-1) -> `karpenter` (0) -> `karpenter-resources` (1).

## Step 7 - Verify

```bash
kubectl -n kube-system get pods -l app.kubernetes.io/name=karpenter
kubectl -n kube-system logs -l app.kubernetes.io/name=karpenter --tail=50
kubectl get ec2nodeclass,nodepool
# READY must be True for both. If an EC2NodeClass is not ready: kubectl describe ec2nodeclass default
```

### Scale test

```bash
kubectl create namespace karpenter-test
kubectl -n karpenter-test create deployment inflate --image=public.ecr.aws/eks-distro/kubernetes/pause:3.7 --replicas=0
kubectl -n karpenter-test set resources deployment inflate --requests=cpu=1
kubectl -n karpenter-test scale deployment inflate --replicas=5

kubectl get nodeclaims -w                    # a NodeClaim appears within seconds
kubectl get nodes -L karpenter.sh/nodepool,node.kubernetes.io/instance-type,karpenter.sh/capacity-type

kubectl -n karpenter-test delete deployment inflate     # node is removed ~1 minute later (consolidateAfter)
kubectl delete namespace karpenter-test
```

### End-to-end with the real app

Turn the HPA on in `apps/weatherapp/envs/dev/values.yaml` (`hpa.enabled: true`, `maxReplicas: 6`), generate load,
and watch: HPA adds pods -> pods go Pending -> Karpenter adds a node -> pods schedule. Grafana (Mimir) already
shows pod and node metrics; scrape Karpenter's metrics on port `8080` with Alloy if you want a dashboard.

## Step 8 - Move off the old node group (optional)

1. Keep the managed node group at 2 nodes (Karpenter, CoreDNS, Argo CD).
2. Cordon and drain the other old nodes one by one: `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`
3. Check the pods come back on Karpenter nodes and the Vault pod is `Running` (its PV is in `eu-west-1a`).
4. Reduce the managed node group's desired/min size.

## Troubleshooting

| Symptom | Check |
|---|---|
| Pods stay Pending, no NodeClaim | `kubectl logs` of Karpenter; pod requests larger than any allowed instance; `cpuLimit` reached; missing toleration for a tainted pool |
| NodeClaim created, node never joins | Node role missing from access entries (`aws eks list-access-entries`); SG or subnet tag wrong; no route to the API / NAT |
| `UnauthorizedOperation` in controller logs | Pod Identity association missing or wrong service account name (`karpenter` in `kube-system`) |
| `EC2NodeClass` not Ready | Subnet/SG tag `karpenter.sh/discovery` not found; role name mismatch |
| Spot capacity errors | Widen `instanceCategories`, or keep `on-demand` as a fallback in `capacityTypes` |
| Nodes churn too often | Raise `consolidateAfter`, add `karpenter.sh/do-not-disrupt` to sensitive pods, tighten `budgets` |
| Vault/DB pod Pending after a move | The stateful pool must allow the zone of the EBS volume (`eu-west-1a`) |
| Argo shows `OutOfSync` on NodePool | Add `ignoreDifferences` for fields Karpenter mutates, or confirm `ServerSideApply` is on |

## Rollback

1. Remove `karpenter-resources.yaml` first (Karpenter drains and terminates its nodes - make sure the managed node group can hold the workloads, or scale it up first).
2. Then remove `karpenter.yaml` / the CRD Application.
3. Finally remove the Terraform module (IAM, SQS, EventBridge).

Deleting the controller before the NodePools leaves orphaned EC2 instances; check `aws ec2 describe-instances --filters Name=tag-key,Values=karpenter.sh/nodepool`.

## Cost notes

- `limits.cpu` on each NodePool is the hard ceiling - start low and raise it.
- Spot is typically 60-70% cheaper; it is fine for `weatherapp`, `redis` and monitoring, not for databases or Vault.
- Consolidation replaces underused nodes with cheaper ones; watch it in the first week.
- Karpenter itself costs nothing; the 2-node managed group is the only fixed cost.

## References

- Docs: <https://karpenter.sh/docs/>
- Getting started with EKS: <https://karpenter.sh/docs/getting-started/>
- Migrating from Cluster Autoscaler: <https://karpenter.sh/docs/getting-started/migrating-from-cas/>
- Terraform module: <https://registry.terraform.io/modules/terraform-aws-modules/eks/aws/latest/submodules/karpenter>

#!/usr/bin/env python3
"""Cross-checks that plain YAML linting cannot do. Exit code 1 on any problem."""
import fnmatch
import glob
import os
import sys

import yaml

errors = []

# the repo these files live in: only its paths can be checked on disk
GITOPS_REPO = "https://gitlab.manovaturi.com/weatherapp/weatherapp-gitops.git"


def err(msg):
    errors.append(msg)


def load(path):
    with open(path, encoding="utf-8") as f:
        return list(yaml.safe_load_all(f))


# AppProject name -> allowed namespace patterns / source repos
projects = {}
for f in glob.glob("argocd/projects/*.yaml"):
    d = load(f)[0]
    projects[d["metadata"]["name"]] = {
        "namespaces": [x["namespace"] for x in d["spec"]["destinations"]],
        "repos": d["spec"]["sourceRepos"],
    }

# --- workloads: apps/*/envs/*/config.yaml
required = ("name", "app", "env", "namespace", "chart", "mesh")
names = set()
for f in sorted(glob.glob("apps/*/envs/*/config.yaml")):
    c = load(f)[0]
    parts = f.replace("\\", "/").split("/")
    app, env = parts[1], parts[3]
    for k in required:
        if k not in c:
            err(f"{f}: missing '{k}'")
    if any(k not in c for k in required):
        continue
    if c["app"] != app:
        err(f"{f}: app '{c['app']}' != folder '{app}'")
    if c["env"] != env:
        err(f"{f}: env '{c['env']}' != folder '{env}'")
    if c["env"] not in projects:
        err(f"{f}: env '{c['env']}' is not an AppProject")
    elif not any(fnmatch.fnmatch(c["namespace"], p) for p in projects[c["env"]]["namespaces"]):
        err(f"{f}: namespace '{c['namespace']}' not allowed by project '{c['env']}'")
    if not os.path.isdir(f"charts/{c['chart']}"):
        err(f"{f}: charts/{c['chart']} does not exist")
    if c["mesh"] not in ("ambient", "none"):
        err(f"{f}: mesh must be ambient or none")
    if c["name"] in names:
        err(f"{f}: duplicate Application name '{c['name']}'")
    names.add(c["name"])

# --- explicit Applications: project exists, values files and chart paths exist
for f in sorted(glob.glob("argocd/platform/**/*.yaml", recursive=True) + glob.glob("argocd/workloads/*.yaml")):
    d = load(f)[0]
    if d.get("kind") != "Application":
        err(f"{f}: expected an Application")
        continue
    name, spec = d["metadata"]["name"], d["spec"]
    if name in names:
        err(f"{f}: duplicate Application name '{name}'")
    names.add(name)
    proj = spec["project"]
    if proj not in projects:
        err(f"{f}: unknown project '{proj}'")
        continue
    ns = spec["destination"]["namespace"]
    if not any(fnmatch.fnmatch(ns, p) for p in projects[proj]["namespaces"]):
        err(f"{f}: namespace '{ns}' not allowed by project '{proj}'")
    for s in spec.get("sources", []) + ([spec["source"]] if "source" in spec else []):
        if "*" not in projects[proj]["repos"] and s["repoURL"] not in projects[proj]["repos"]:
            err(f"{f}: repo {s['repoURL']} not allowed by project '{proj}'")
        # a path in another repo (the Gateway API CRDs) cannot be checked here
        if "path" in s and s["repoURL"] == GITOPS_REPO and not os.path.isdir(s["path"]):
            err(f"{f}: path {s['path']} does not exist")
        for v in s.get("helm", {}).get("valueFiles", []):
            if v.startswith("$values/"):
                target = v[len("$values/"):]
            else:
                target = os.path.normpath(os.path.join(s.get("path", "."), v))
            if not os.path.exists(target) and not s.get("helm", {}).get("ignoreMissingValueFiles"):
                err(f"{f}: values file {target} does not exist")

# --- orphan values: platform/<group>/<name>/values.yaml with no Application of that name
for f in glob.glob("platform/*/*/values.yaml"):
    n = f.replace("\\", "/").split("/")[2]
    if n not in names:
        err(f"{f}: no Application named '{n}'")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"ok: {len(names)} applications checked")

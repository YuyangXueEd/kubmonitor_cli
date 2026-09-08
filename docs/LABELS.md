# Workload Ownership Labels

kubmonitor identifies who owns each workload in a shared namespace by
reading Kubernetes labels. Job generators (e.g. a group's template repo)
should stamp these on **both** the Job's `metadata.labels` and the pod
`spec.template.metadata.labels`.

## Labels

| Label | Required | Example | Meaning |
|---|---|---|---|
| `owner` | **yes** | `rasin` | The cluster account that owns this workload (login account name, not a display name). |
| `project` | **yes** | `eidf105` | The project the workload belongs to. |
| `purpose` | recommended | `batch` | One of `batch`, `interactive`, `serving`. |

Label *values* may contain letters, digits, `-`, `_` and `.` (max 63
chars), so accounts like `ada_lovelace` are valid values — note that
underscores are **not** valid in resource *names*, which is why labels
beat name conventions for identifying owners.

A custom prefix (e.g. `example.org/owner`) is supported via the
`label_prefix` project-config option and `kubmonitor validate --prefix`;
the default contract is unprefixed.

## Example

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  generateName: cuda-rasin-
  labels:
    kueue.x-k8s.io/queue-name: eidf105ns-user-queue
    owner: rasin
    project: eidf105
    purpose: batch
spec:
  template:
    metadata:
      labels:
        app: cuda
        owner: rasin
        project: eidf105
        purpose: batch
    spec:
      ...
```

## How kubmonitor resolves the owner

1. `owner` label (workload's own labels, then its pod template's);
2. name matching: a `-`-separated token of the Job/Pod name equal to a
   known account — or a declared `aliases:` nickname — from the project's
   members file (underscores in account names are compared against `-`
   in names);
3. image matching: the Docker-Hub-style account segment or the image tag,
   but only when it matches a declared account/alias from the members
   file — an arbitrary image name is never trusted on its own;
4. otherwise the workload is listed separately as *unattributed*, never
   silently dropped.

The resolved *account* maps to a *person* via the members file
(`examples/members.yaml`), so one person with several accounts appears
as a single row in reports.

## Checking a manifest

```bash
kubmonitor validate path/to/job.yaml
```

exits non-zero and prints what's missing if the manifest lacks the
required labels (or still contains unfilled `<PLACEHOLDER>`s).

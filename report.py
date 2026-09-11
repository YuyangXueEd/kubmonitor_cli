"""Usage reports over the SQLite DB written by `kubmonitor collect`.

    kubmonitor report --config project.yaml [--month 2026-09]
                      [--from 2026-09-01 --to 2026-09-30]
                      [--csv usage.csv] [--html usage.html]

GPU·hours are *allocation* hours (requested GPUs x wall time inside the
reporting window, clipped at its edges). Real utilization percentages —
if the collector samples them — are shown alongside, so "held 4 GPUs at
3% util" is visible, not hidden.
"""

import calendar
import csv as csv_mod
from datetime import datetime, timedelta, timezone

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import usagedb
from kmconfig import ConfigError


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_window(month=None, date_from=None, date_to=None):
    """Return (start, end, label) as aware UTC datetimes.

    Raises ConfigError on malformed inputs so the CLI reports a friendly
    error instead of a traceback.
    """
    now = datetime.now(timezone.utc)
    if month:
        try:
            year, mon = (int(p) for p in month.split("-"))
            start = datetime(year, mon, 1, tzinfo=timezone.utc)
        except ValueError:
            raise ConfigError(
                f"invalid --month {month!r} (expected YYYY-MM, e.g. 2026-09)")
        last_day = calendar.monthrange(year, mon)[1]
        end = datetime(year, mon, last_day, 23, 59, 59, tzinfo=timezone.utc)
        return start, min(end, now), month
    if date_from or date_to:
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if date_from:
            start = _parse_ts(date_from + "T00:00:00Z")
            if start is None:
                raise ConfigError(
                    f"invalid --from {date_from!r} (expected YYYY-MM-DD)")
        end = now
        if date_to:
            end = _parse_ts(date_to + "T23:59:59Z")
            if end is None:
                raise ConfigError(
                    f"invalid --to {date_to!r} (expected YYYY-MM-DD)")
        label = f"{date_from or 'beginning'} .. {date_to or 'now'}"
        return start, min(end, now), label
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return start, now, f"{now.year}-{now.month:02d} (to date)"


def _overlap_hours(start, end, w_start, w_end):
    lo, hi = max(start, w_start), min(end, w_end)
    if hi <= lo:
        return 0.0
    return (hi - lo).total_seconds() / 3600.0


def _bar(fraction, width=24, color="cyan"):
    filled = max(0, min(width, round(fraction * width)))
    return (f"[{color}]{'█' * filled}[/{color}]"
            f"[dim]{'░' * (width - filled)}[/dim]")


def build_report_data(conn, cfg, members, w_start, w_end):
    """Aggregate the DB into plain dicts (also reused by --csv)."""
    rows = conn.execute(
        "SELECT * FROM workloads WHERE project = ?", (cfg.project,)).fetchall()

    per_person = {}   # key -> aggregate dict
    per_model = {}
    per_purpose = {}
    unattributed = []
    top_workloads = []

    for row in rows:
        started = _parse_ts(row["started_at"])
        start = started or _parse_ts(row["created_at"])
        if start is None:
            continue
        end = _parse_ts(row["completed_at"]) or _parse_ts(row["last_seen"])
        if end is None or end < start:
            end = start
        hours = _overlap_hours(start, end, w_start, w_end)
        if hours <= 0:
            continue
        # GPUs are only held once the workload actually started — a job
        # queued for days must not accrue GPU·hours while Pending.
        run_hours = (_overlap_hours(started, end, w_start, w_end)
                     if started else 0.0)
        gpu_hours = run_hours * (row["gpu_count"] or 0)

        account = row["account"]
        person = members.person_for(account) if account else None
        key = person.name if person else (account or "(unattributed)")
        agg = per_person.setdefault(key, {
            "person": key,
            "email": person.email if person else "",
            "accounts": set(), "jobs": 0, "gpu_jobs": 0,
            "gpu_hours": 0.0, "wall_hours": 0.0,
            "wait_seconds": [], "attributed": account is not None,
        })
        if account:
            agg["accounts"].add(account)
        agg["jobs"] += 1
        agg["wall_hours"] += hours
        if (row["gpu_count"] or 0) > 0:
            agg["gpu_jobs"] += 1
            agg["gpu_hours"] += gpu_hours
        created = _parse_ts(row["created_at"])
        started = _parse_ts(row["started_at"])
        if created and started and started >= created:
            agg["wait_seconds"].append((started - created).total_seconds())

        if gpu_hours > 0:
            model = row["gpu_model"] or "unknown"
            per_model[model] = per_model.get(model, 0.0) + gpu_hours
            purpose = row["purpose"] or "unspecified"
            per_purpose[purpose] = per_purpose.get(purpose, 0.0) + gpu_hours
            top_workloads.append({
                "name": row["name"], "kind": row["kind"], "owner": key,
                "gpu_count": row["gpu_count"], "gpu_model": model,
                "gpu_hours": gpu_hours, "phase": row["phase"],
            })
        if account is None:
            unattributed.append({
                "name": row["name"], "kind": row["kind"],
                "gpu_hours": gpu_hours, "phase": row["phase"],
                "last_seen": row["last_seen"],
            })

    # Mean sampled GPU utilization per account -> merge to person key.
    util_by_key = {}
    w_start_s = w_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    w_end_s = w_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in conn.execute(
            "SELECT account, AVG(util_pct) AS u, COUNT(*) AS n "
            "FROM util_samples WHERE project = ? AND ts BETWEEN ? AND ? "
            "GROUP BY account", (cfg.project, w_start_s, w_end_s)):
        account = row["account"]
        person = members.person_for(account) if account else None
        key = person.name if person else (account or "(unattributed)")
        prev = util_by_key.get(key)
        if prev:
            total = prev["n"] + row["n"]
            util_by_key[key] = {
                "u": (prev["u"] * prev["n"] + row["u"] * row["n"]) / total,
                "n": total}
        else:
            util_by_key[key] = {"u": row["u"], "n": row["n"]}

    people = sorted(per_person.values(),
                    key=lambda a: a["gpu_hours"], reverse=True)
    for agg in people:
        agg["accounts"] = sorted(agg["accounts"])
        waits = agg.pop("wait_seconds")
        agg["avg_wait_min"] = (sum(waits) / len(waits) / 60) if waits else None
        util = util_by_key.get(agg["person"])
        agg["avg_util_pct"] = util["u"] if util else None

    top_workloads.sort(key=lambda w: w["gpu_hours"], reverse=True)

    # Idle GPU allocations: running for a while, but the sampled
    # utilization says nobody is actually using the GPUs — the classic
    # "job started only to kubectl-exec in later" pattern.
    idle = []
    now = datetime.now(timezone.utc)
    sample_cutoff = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # One aggregation pass over the window, joined to workloads by the
    # owning uid the collector stamps on each sample (exact, no name
    # guessing; also avoids one query per candidate workload).
    util_by_wl = {r["workload_uid"]: r for r in conn.execute(
        "SELECT workload_uid, AVG(util_pct) AS u, "
        "COUNT(util_pct) AS n "        # count only non-NULL readings
        "FROM util_samples WHERE project = ? AND ts >= ? "
        "AND workload_uid IS NOT NULL GROUP BY workload_uid",
        (cfg.project, sample_cutoff))}
    # Transition fallback for samples recorded before workload_uid
    # existed: pre-aggregated per pod in SQL, then each pod maps to a
    # workload only when stripping exactly one '-suffix' segment (or
    # nothing, for bare pods) lands on a known workload name — a bare
    # prefix match would also swallow another job named <job>-worker.
    legacy_by_name = {}
    legacy_groups = conn.execute(
        "SELECT pod_name, AVG(util_pct) AS u, COUNT(util_pct) AS n "
        "FROM util_samples WHERE project = ? AND ts >= ? "
        "AND workload_uid IS NULL AND util_pct IS NOT NULL "
        "GROUP BY pod_name", (cfg.project, sample_cutoff)).fetchall()
    if legacy_groups:
        workload_names = {row["name"] for row in rows}
        for grp in legacy_groups:
            pod_name = grp["pod_name"]
            if pod_name in workload_names:
                target = pod_name
            else:
                parent = pod_name.rsplit("-", 1)[0]
                target = parent if parent in workload_names else None
            if target:
                acc = legacy_by_name.setdefault(target, [0.0, 0])
                acc[0] += grp["u"] * grp["n"]
                acc[1] += grp["n"]

    def _legacy_stats(name):
        acc = legacy_by_name.get(name)
        return (acc[0] / acc[1], acc[1]) if acc else (None, 0)

    for row in rows:
        if (row["gpu_count"] or 0) <= 0 or row["phase"] != "Running":
            continue
        started = _parse_ts(row["started_at"])
        last_seen = _parse_ts(row["last_seen"])
        if not started or not last_seen:
            continue
        if (now - last_seen).total_seconds() > 3600:
            continue  # not seen recently; probably already gone
        age_h = (now - started).total_seconds() / 3600
        if age_h < 6:
            continue  # too young to judge
        hit = util_by_wl.get(row["uid"])
        if hit:
            mean_u, n = hit["u"], hit["n"]
        else:
            mean_u, n = _legacy_stats(row["name"])
        if mean_u is not None and n >= 3 and mean_u < 15:
            account = row["account"]
            person = members.person_for(account) if account else None
            idle.append({
                "name": row["name"],
                "owner": person.name if person else (account or "(unattributed)"),
                "gpu_count": row["gpu_count"],
                "gpu_model": row["gpu_model"] or "?",
                "age_h": age_h,
                "util": mean_u,
                "purpose": row["purpose"] or "unspecified",
            })
    idle.sort(key=lambda w: w["gpu_count"] * w["age_h"], reverse=True)

    # Data coverage inside the window.
    runs = conn.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last "
        "FROM collect_runs WHERE project = ? AND ok = 1 "
        "AND ts BETWEEN ? AND ?",
        (cfg.project, w_start_s, w_end_s)).fetchone()
    last_ever = usagedb.last_collect_ts(conn, cfg.project)

    active_people = {a["person"] for a in people}
    silent_members = [p.name for p in members.people
                      if p.status == "active" and p.name not in active_people]

    return {
        "people": people,
        "models": sorted(per_model.items(), key=lambda kv: kv[1], reverse=True),
        "purposes": sorted(per_purpose.items(), key=lambda kv: kv[1],
                           reverse=True),
        "top_workloads": top_workloads[:10],
        "unattributed": unattributed,
        "idle_gpus": idle,
        "coverage": {"runs": runs["n"], "first": runs["first"],
                     "last": runs["last"], "last_ever": last_ever},
        "silent_members": silent_members,
    }


def render_report(console, cfg, data, window_label):
    people = data["people"]
    total_gpu_hours = sum(a["gpu_hours"] for a in people)

    cov = data["coverage"]
    cov_line = (f"collect runs in window: {cov['runs']}"
                if cov["runs"] else "[red]no collect runs in window[/red]")
    warn = ""
    last = _parse_ts(cov["last_ever"])
    if last is None:
        warn = "\n[bold red]⚠ no successful collect run recorded — " \
               "is the timer running?[/bold red]"
    else:
        age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if age_min > 30:
            warn = (f"\n[bold red]⚠ last successful collect was "
                    f"{age_min / 60:.1f}h ago — data may be stale[/bold red]")

    console.print(Panel(
        f"[bold]GPU usage report[/bold] — project [cyan]{cfg.project}[/cyan]"
        f" (namespace {cfg.namespace})\n"
        f"window: [bold]{window_label}[/bold]   "
        f"total: [bold yellow]{total_gpu_hours:,.1f} GPU·h[/bold yellow]   "
        f"{cov_line}{warn}",
        border_style="blue"))

    if people:
        table = Table(title="GPU·hours by person", box=box.SIMPLE_HEAD,
                      expand=True)
        table.add_column("Person", style="bold")
        table.add_column("Accounts", style="magenta")
        table.add_column("GPU·h", justify="right", style="yellow")
        table.add_column("", min_width=26)
        table.add_column("Jobs", justify="right")
        table.add_column("Avg wait", justify="right")
        table.add_column("Avg util", justify="right")
        max_hours = max(a["gpu_hours"] for a in people) or 1.0
        for agg in people:
            if agg["gpu_hours"] <= 0 and agg["jobs"] == 0:
                continue
            wait = (f"{agg['avg_wait_min']:.0f}m"
                    if agg["avg_wait_min"] is not None else "-")
            if agg["avg_util_pct"] is None:
                util = "[dim]-[/dim]"
            else:
                pct = agg["avg_util_pct"]
                color = "green" if pct >= 60 else \
                        "yellow" if pct >= 25 else "red"
                util = f"[{color}]{pct:.0f}%[/{color}]"
            name = agg["person"] if agg["attributed"] \
                else f"[red]{agg['person']}[/red]"
            table.add_row(
                name, ",".join(agg["accounts"]),
                f"{agg['gpu_hours']:,.1f}",
                _bar(agg["gpu_hours"] / max_hours),
                str(agg["jobs"]), wait, util)
        console.print(table)
    else:
        console.print("[dim]No workloads recorded in this window.[/dim]")

    if data["models"]:
        table = Table(title="GPU·hours by GPU model", box=box.SIMPLE_HEAD)
        table.add_column("Model", style="cyan")
        table.add_column("GPU·h", justify="right", style="yellow")
        table.add_column("", min_width=26)
        top = data["models"][0][1] or 1.0
        for model, hours in data["models"]:
            table.add_row(model, f"{hours:,.1f}", _bar(hours / top,
                                                       color="green"))
        console.print(table)

    if data["purposes"] and not (
            len(data["purposes"]) == 1
            and data["purposes"][0][0] == "unspecified"):
        table = Table(title="GPU·hours by purpose", box=box.SIMPLE_HEAD)
        table.add_column("Purpose", style="cyan")
        table.add_column("GPU·h", justify="right", style="yellow")
        for purpose, hours in data["purposes"]:
            table.add_row(purpose, f"{hours:,.1f}")
        console.print(table)

    if data["top_workloads"]:
        table = Table(title="Top workloads", box=box.SIMPLE_HEAD, expand=True)
        table.add_column("Workload", style="cyan", no_wrap=True)
        table.add_column("Owner")
        table.add_column("GPUs", justify="right")
        table.add_column("Model")
        table.add_column("GPU·h", justify="right", style="yellow")
        table.add_column("Phase")
        for w in data["top_workloads"]:
            table.add_row(w["name"], w["owner"], str(w["gpu_count"]),
                          w["gpu_model"], f"{w['gpu_hours']:,.1f}",
                          w["phase"])
        console.print(table)

    if data["idle_gpus"]:
        table = Table(
            title="[yellow]Idle GPU allocations[/yellow] "
                  "(running >6h, mean sampled util <15% over last 24h)",
            box=box.SIMPLE_HEAD, expand=True)
        table.add_column("Workload", style="cyan", no_wrap=True)
        table.add_column("Owner")
        table.add_column("GPUs", justify="right", style="yellow")
        table.add_column("Model")
        table.add_column("Purpose")
        table.add_column("Running", justify="right")
        table.add_column("Avg util", justify="right", style="red")
        for w in data["idle_gpus"]:
            table.add_row(
                w["name"], w["owner"], str(w["gpu_count"]), w["gpu_model"],
                w["purpose"], f"{w['age_h']:.0f}h", f"{w['util']:.0f}%")
        console.print(table)
        console.print(
            "[dim]These GPUs are allocated but barely used — often a job "
            "kept alive just for kubectl exec. Worth a friendly nudge.[/dim]")

    if data["unattributed"]:
        table = Table(
            title="[red]Unattributed workloads[/red] "
                  "(no owner label, name matched no known account)",
            box=box.SIMPLE_HEAD, expand=True)
        table.add_column("Workload", style="red", no_wrap=True)
        table.add_column("Kind")
        table.add_column("GPU·h", justify="right")
        table.add_column("Phase")
        table.add_column("Last seen")
        for w in data["unattributed"][:15]:
            table.add_row(w["name"], w["kind"], f"{w['gpu_hours']:,.1f}",
                          w["phase"], w["last_seen"])
        extra = len(data["unattributed"]) - 15
        console.print(table)
        if extra > 0:
            console.print(f"[dim]... and {extra} more[/dim]")
        console.print(
            "[dim]Fix: deploy via the group templates, add an `owner` "
            "label (docs/LABELS.md), or declare an alias in "
            "members.yaml.[/dim]")

    if data["silent_members"]:
        console.print(
            f"[dim]Members with no recorded usage in this window: "
            f"{', '.join(data['silent_members'])}[/dim]")


def export_csv(path, data, window_label):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.writer(f)
        writer.writerow(["window", "person", "email", "accounts", "jobs",
                         "gpu_jobs", "gpu_hours", "wall_hours",
                         "avg_wait_min", "avg_util_pct"])
        for agg in data["people"]:
            writer.writerow([
                window_label, agg["person"], agg["email"],
                ";".join(agg["accounts"]), agg["jobs"], agg["gpu_jobs"],
                f"{agg['gpu_hours']:.2f}", f"{agg['wall_hours']:.2f}",
                f"{agg['avg_wait_min']:.1f}"
                if agg["avg_wait_min"] is not None else "",
                f"{agg['avg_util_pct']:.1f}"
                if agg["avg_util_pct"] is not None else "",
            ])


def run_report(cfg, month=None, date_from=None, date_to=None,
               csv_path=None, html_path=None):
    w_start, w_end, label = parse_window(month, date_from, date_to)
    conn = usagedb.open_db(cfg.db)
    try:
        members = cfg.load_members()
        data = build_report_data(conn, cfg, members, w_start, w_end)
    finally:
        conn.close()

    from monitor import build_theme
    console = Console(record=bool(html_path), theme=build_theme())
    render_report(console, cfg, data, label)
    if csv_path:
        export_csv(csv_path, data, label)
        console.print(f"[dim]CSV written to {csv_path}[/dim]")
    if html_path:
        console.save_html(html_path)
        print(f"HTML written to {html_path}")
    return data

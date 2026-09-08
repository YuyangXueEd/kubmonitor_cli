"""Argument handling for the accounting subcommands.

`kubmonitor <namespace>` (the TUI) stays in monitor.py; this module owns
`kubmonitor collect|report|validate`.
"""

import argparse
import sys

import yaml

from kmconfig import ProjectConfig, ConfigError, find_default_config


def _load_config(args):
    """Resolve the project config from --config / env / defaults,
    with --db allowing a config-less run (mock tests, ad-hoc)."""
    path = getattr(args, "config", None) or find_default_config()
    if path:
        cfg = ProjectConfig.load(path)
        if getattr(args, "db", None):
            cfg.db = args.db
        return cfg
    if getattr(args, "db", None):
        return ProjectConfig(
            project=getattr(args, "project", None) or "adhoc",
            namespace=getattr(args, "namespace", None) or "default",
            db=args.db)
    raise ConfigError(
        "no project config found — pass --config, set KUBMONITOR_CONFIG, "
        "or create ~/.config/kubmonitor/project.yaml "
        "(see examples/project.yaml)")


def _cmd_collect(argv):
    parser = argparse.ArgumentParser(prog="kubmonitor collect")
    parser.add_argument("--config", help="project config yaml")
    parser.add_argument("--db", help="SQLite path (overrides the config's; "
                                     "with no config, enables an ad-hoc run)")
    parser.add_argument("--project", help="project id (with --db only)")
    parser.add_argument("--namespace", help="namespace (with --db only)")
    parser.add_argument("--mock", action="store_true",
                        help="collect from generated mock data (testing)")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    from collector import collect
    cfg = _load_config(args)
    collect(cfg, use_mock=args.mock, verbose=not args.quiet)
    return 0


def _cmd_report(argv):
    parser = argparse.ArgumentParser(prog="kubmonitor report")
    parser.add_argument("--config", help="project config yaml")
    parser.add_argument("--db", help="override SQLite path")
    parser.add_argument("--project", help="project id (with --db only)")
    parser.add_argument("--month", help="calendar month, e.g. 2026-09")
    parser.add_argument("--from", dest="date_from",
                        help="window start, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="window end, YYYY-MM-DD")
    parser.add_argument("--csv", help="also write per-person CSV here")
    parser.add_argument("--html", help="also write the report as HTML here")
    args = parser.parse_args(argv)

    from report import run_report
    cfg = _load_config(args)
    run_report(cfg, month=args.month, date_from=args.date_from,
               date_to=args.date_to, csv_path=args.csv, html_path=args.html)
    return 0


def _validate_doc(doc, prefix, errors, warnings, source):
    kind = doc.get("kind")
    if kind not in ("Job", "Pod", "Deployment", "StatefulSet"):
        return False

    def key_of(name):
        return f"{prefix}/{name}" if prefix else name

    def check(labels, where):
        for key in (key_of("owner"), key_of("project")):
            value = (labels or {}).get(key)
            if not value:
                errors.append(f"{source}: {where} missing label '{key}'")
            elif "<" in str(value):
                errors.append(
                    f"{source}: {where} label '{key}' still has an "
                    f"unfilled placeholder: {value}")
        if not (labels or {}).get(key_of("purpose")):
            warnings.append(
                f"{source}: {where} has no '{key_of('purpose')}' label "
                f"(batch|interactive|serving) — recommended")

    meta_labels = (doc.get("metadata") or {}).get("labels")
    check(meta_labels, f"{kind} metadata")
    if kind != "Pod":
        tpl = ((doc.get("spec") or {}).get("template") or {})
        check((tpl.get("metadata") or {}).get("labels"),
              f"{kind} pod template")
    return True


def _cmd_validate(argv):
    parser = argparse.ArgumentParser(prog="kubmonitor validate")
    parser.add_argument("files", nargs="+", help="manifest yaml file(s)")
    parser.add_argument("--prefix", default="",
                        help="optional label prefix of the contract "
                             "(default: unprefixed owner/project/purpose)")
    args = parser.parse_args(argv)

    errors, warnings = [], []
    checked = 0
    for path in args.files:
        try:
            with open(path, encoding="utf-8") as f:
                docs = list(yaml.safe_load_all(f))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{path}: cannot parse: {exc}")
            continue
        for doc in docs:
            if isinstance(doc, dict) and _validate_doc(
                    doc, args.prefix, errors, warnings, path):
                checked += 1

    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if not checked and not errors:
        print("warning: no Job/Pod/Deployment documents found to check")
    if errors:
        return 1
    print(f"OK: {checked} workload document(s) carry the required "
          f"ownership labels")
    return 0


def run_subcommand(name, argv):
    handlers = {"collect": _cmd_collect, "report": _cmd_report,
                "validate": _cmd_validate}
    try:
        return handlers[name](argv)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

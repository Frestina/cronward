"""Where cronward's files are, and its config.

The config is ~/.config/cronward/config.toml ($XDG_CONFIG_HOME), described in
SPEC.md and in TEMPLATE, which `cronward init` writes. Unknown keys are
errors, so a typo is caught instead of quietly ignored. A config with no
jobs is fine: a viewer needs none.

`cronward add` and `remove` change the file's text, not a parsed copy, so
comments stay. Every change is read back before it is saved.
"""

import dataclasses
import datetime
import json
import os
import re
import shlex
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

from cronward import cron

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
WORKFLOW = re.compile(r"[A-Za-z0-9._-]+\.ya?ml|[0-9]+")
DURATION = re.compile(r"(\d+)([mhd])")
UNITS = {"m": 60, "h": 3600, "d": 86400}
URGENCIES = ("low", "normal", "critical")
FINISH_WITHIN = 6 * 3600

TEMPLATE = """\
# cronward: starts GitHub Actions workflows on a schedule and says whether
# they passed. Full description: https://github.com/Frestina/cronward
#
# After editing, `cronward doctor` checks everything.

# Time zone for schedules: "UTC" (as in GitHub's schedule), "local", or a
# name like "Europe/Oslo". A job can set its own.
timezone = "UTC"

# A run that has not finished this long after its start counts as didn't run.
finish_within = "6h"

# GitHub turns off scheduled workflows in public repos after 60 days without
# activity; once a workflow's schedule: lines are out, it no longer applies.
# true: cronward turns such a workflow back on and tells you. false: it only
# tells you. A workflow you turned off yourself is never turned on.
reenable = true

# One account per token. A fine-grained token belongs to one user or
# organisation, so each owner of repos below needs its own. Store the token
# with: cronward token set <account>
[accounts.personal]

# One job per workflow. The name after "jobs." is what commands and
# notifications call it. The workflow needs `workflow_dispatch:` under `on:`.
# [jobs.nightly-check]
# account = "personal"
# repo = "me/weather-widget"
# workflow = "check.yml"        # file name in .github/workflows, or its ID
# schedule = "0 6 * * *"        # minute hour day-of-month month day-of-week
# ref = "main"                  # default: the repo's default branch
# inputs = { level = "full" }
# timezone = "Europe/Oslo"
# finish_within = "2h"
# enabled = false

[notify]
# A desktop notification on this machine when a result changes.
desktop = true
# low, normal or critical. Omarchy hides all but critical after 30 seconds.
urgency = "critical"
# A command run with each notice as JSON on stdin, such as a script that
# posts to your phone. A string is split like a shell would split it.
# command = ["/home/me/bin/post-to-ntfy"]
"""


class ConfigError(Exception):
    """The config does not read; says why."""


def _home(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


def config_home() -> Path:
    return _home("XDG_CONFIG_HOME", ".config")


def config_path() -> Path:
    return config_home() / "cronward" / "config.toml"


def state_dir() -> Path:
    return _home("XDG_STATE_HOME", ".local/state") / "cronward"


def data_dir() -> Path:
    return _home("XDG_DATA_HOME", ".local/share") / "cronward"


@dataclass(frozen=True)
class Job:
    name: str
    account: str
    repo: str
    workflow: str
    schedule: cron.Schedule
    zone: datetime.tzinfo
    zone_name: str
    ref: str | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    finish_within: int = FINISH_WITHIN
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    accounts: tuple[str, ...] = ()
    jobs: tuple[Job, ...] = ()
    reenable: bool = True
    desktop: bool = True
    urgency: str = "critical"
    command: tuple[str, ...] = ()

    def job(self, name: str) -> Job:
        for j in self.jobs:
            if j.name == name:
                return j
        raise ConfigError(f"no job {name!r} in {config_path()}"
                          + (f"; there are: {', '.join(j.name for j in self.jobs)}"
                             if self.jobs else ""))


def duration(text: object, where: str) -> int:
    m = DURATION.fullmatch(text) if isinstance(text, str) else None
    if not m or int(m.group(1)) == 0:
        raise ConfigError(f"{where}: {text!r} is not a time like \"30m\", \"6h\" or \"1d\"")
    return int(m.group(1)) * UNITS[m.group(2)]


def zone(name: object, where: str) -> tuple[datetime.tzinfo, str]:
    if not isinstance(name, str):
        raise ConfigError(f"{where}: {name!r} is not a time zone name")
    if name.upper() == "UTC":
        return datetime.timezone.utc, "UTC"
    if name == "local":
        return local_zone()
    try:
        return zoneinfo.ZoneInfo(name), name
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"{where}: no time zone {name!r}; use \"UTC\", \"local\" or a "
                          "name like \"Europe/Oslo\"") from None


def local_zone() -> tuple[datetime.tzinfo, str]:
    """This machine's zone, from /etc/localtime, with its daylight saving rules."""
    path = Path("/etc/localtime")
    real = os.path.realpath(path)
    if "/zoneinfo/" in real:
        name = real.split("/zoneinfo/", 1)[1]
        try:
            return zoneinfo.ZoneInfo(name), name
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    try:
        with open(path, "rb") as f:
            return zoneinfo.ZoneInfo.from_file(f, key="local"), "local"
    except (OSError, ValueError):
        return datetime.timezone.utc, "UTC"


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    odd = sorted(set(table) - allowed)
    if odd:
        raise ConfigError(f"{where}: unknown setting{'s' if len(odd) > 1 else ''} "
                          f"{', '.join(odd)}; known: {', '.join(sorted(allowed))}")


def _typed(value: object, kind: type, what: str, where: str):
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{where}: {what} must be {kind.__name__}, not {value!r}")
    return value


def _command(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value))
        except ValueError as e:
            raise ConfigError(f"[notify] command: {e}") from None
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ConfigError(f"[notify] command must be a string or a list of strings, not {value!r}")


def _job(name: str, table: object, accounts: tuple[str, ...], tz: tuple, finish: int) -> Job:
    where = f"[jobs.{name}]"
    if not NAME.fullmatch(name):
        raise ConfigError(f"{where}: a job name is letters, digits, '.', '_' and '-'")
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    _check_keys(table, {"account", "repo", "workflow", "schedule", "ref", "inputs", "timezone",
                        "finish_within", "enabled"}, where)
    for key in ("account", "repo", "workflow", "schedule"):
        if key not in table:
            raise ConfigError(f"{where}: {key} is missing")
    account = _typed(table["account"], str, "account", where)
    if account not in accounts:
        raise ConfigError(f"{where}: no account {account!r}; add [accounts.{account}]")
    repo = _typed(table["repo"], str, "repo", where)
    if not REPO.fullmatch(repo):
        raise ConfigError(f"{where}: repo {repo!r} is not owner/name")
    workflow = table["workflow"]
    if isinstance(workflow, int) and not isinstance(workflow, bool):
        workflow = str(workflow)
    if not isinstance(workflow, str) or not WORKFLOW.fullmatch(workflow):
        raise ConfigError(f"{where}: workflow {workflow!r} is neither a file name like "
                          "\"check.yml\" nor a workflow ID")
    try:
        schedule = cron.parse(_typed(table["schedule"], str, "schedule", where))
    except ValueError as e:
        raise ConfigError(f"{where}: schedule {e}") from None
    ref = table.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise ConfigError(f"{where}: ref must be a branch or tag name")
    inputs = _typed(table.get("inputs", {}), dict, "inputs", where)
    clean = {}
    for k, v in inputs.items():
        if isinstance(v, bool):
            clean[k] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            clean[k] = str(v)
        else:
            raise ConfigError(f"{where}: input {k} must be text, a number or true/false")
    if len(clean) > 25:
        raise ConfigError(f"{where}: GitHub takes at most 25 inputs")
    zone_, zone_name = zone(table["timezone"], where) if "timezone" in table else tz
    return Job(
        name=name, account=account, repo=repo, workflow=workflow, schedule=schedule,
        zone=zone_, zone_name=zone_name, ref=ref, inputs=clean,
        finish_within=(duration(table["finish_within"], f"{where} finish_within")
                       if "finish_within" in table else finish),
        enabled=_typed(table.get("enabled", True), bool, "enabled", where),
    )


def parse(text: str, where: str = "config") -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{where} does not read as TOML: {e}") from None
    _check_keys(data, {"timezone", "finish_within", "reenable", "accounts", "jobs", "notify"},
                where)
    tz = zone(data["timezone"], "timezone") if "timezone" in data else (datetime.timezone.utc,
                                                                          "UTC")
    finish = (duration(data["finish_within"], "finish_within") if "finish_within" in data
              else FINISH_WITHIN)
    accounts_table = _typed(data.get("accounts", {}), dict, "accounts", where)
    for name, table in accounts_table.items():
        if not NAME.fullmatch(name):
            raise ConfigError(f"[accounts.{name}]: an account name is letters, digits, '.', "
                              "'_' and '-'")
        _check_keys(_typed(table, dict, "an account", f"[accounts.{name}]"), set(),
                    f"[accounts.{name}]")
    accounts = tuple(accounts_table)
    jobs = tuple(_job(name, table, accounts, tz, finish)
                 for name, table in _typed(data.get("jobs", {}), dict, "jobs", where).items())
    notify = _typed(data.get("notify", {}), dict, "notify", where)
    _check_keys(notify, {"desktop", "urgency", "command"}, "[notify]")
    urgency = notify.get("urgency", "critical")
    if urgency not in URGENCIES:
        raise ConfigError(f"[notify] urgency must be one of {', '.join(URGENCIES)}")
    return Config(
        accounts=accounts, jobs=jobs,
        reenable=_typed(data.get("reenable", True), bool, "reenable", where),
        desktop=_typed(notify.get("desktop", True), bool, "desktop", "[notify]"),
        urgency=urgency,
        command=_command(notify["command"]) if notify.get("command") else (),
    )


def load(required: bool = True) -> Config:
    """The config; with required=False, the defaults when there is none."""
    path = config_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        if required:
            raise ConfigError(f"no config at {path}; write one with: cronward init") from None
        return Config()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    return parse(text, str(path))


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml(k)} = {_toml(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)  # a TOML basic string


def with_job(text: str, name: str, settings: dict[str, object]) -> str:
    """The config text with [jobs.<name>] added at the end, read back first."""
    if not NAME.fullmatch(name):
        raise ConfigError(f"{name!r} is not a job name: letters, digits, '.', '_' and '-'")
    block = "\n".join([f"[jobs.{name}]", *(f"{k} = {_toml(v)}" for k, v in settings.items())])
    new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    if name not in {j.name for j in parse(new).jobs}:
        raise ConfigError(f"[jobs.{name}] does not read back from the config; add it by hand")
    return new


def _comparable(cfg: Config) -> Config:
    # A zone read from /etc/localtime is a new object each time.
    return dataclasses.replace(cfg, jobs=tuple(dataclasses.replace(j, zone=None)
                                                for j in cfg.jobs))


def without_job(text: str, name: str) -> str:
    """The config text with [jobs.<name>] taken out, up to the next table.
    Comments right above the next table stay with it. Refused unless the
    rest of the config reads exactly as before."""
    lines = text.splitlines(keepends=True)
    quoted = re.escape(name)
    header = re.compile(rf'\s*\[\s*jobs\s*\.\s*(?:{quoted}|"{quoted}")\s*\]\s*(?:#.*)?$')
    starts = [i for i, line in enumerate(lines) if header.match(line)]
    if len(starts) != 1:
        raise ConfigError(f"cannot find one [jobs.{name}] in the config; take it out by hand")
    start = starts[0]
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip()
                               or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    new = "".join(lines[:start] + lines[end:])
    before = parse(text)
    want = dataclasses.replace(before, jobs=tuple(j for j in before.jobs if j.name != name))
    try:
        same = _comparable(parse(new)) == _comparable(want)
    except ConfigError:
        same = False
    if not same:
        raise ConfigError(f"taking out [jobs.{name}] would change more of the config; take it "
                          "out by hand")
    return new


def save(text: str) -> None:
    """Replaces the config atomically. Writes through a symlink, so a config
    kept in a dotfiles repo stays there."""
    real = config_path().resolve()
    tmp = real.with_name(f".{real.name}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, real.stat().st_mode & 0o7777)
    os.replace(tmp, real)

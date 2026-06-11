"""v3 CLI (Click). Phase 0: ``init-db``, ``doctor``, ``status``.

Discovery/score/apply commands arrive with the Phase 1 vertical slice.
"""

from __future__ import annotations

import sys

import click

# The Windows console defaults to cp1252 and raises UnicodeEncodeError on non-ASCII
# (job titles, company names, JD text). Emit UTF-8 with replacement so output never
# crashes the command, regardless of the terminal's codepage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from auto_applier import __version__
from auto_applier.config import load_settings
from auto_applier.db import init_app_db
from auto_applier.doctor import Status, fail_count, run_doctor
from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.job_family import JobFamily
from auto_applier.domain.state import JobState
from auto_applier.telemetry import EventSink, attach_mirror_from_settings, configure_sink

# ASCII-only markers — the Windows console (cp1252) can't encode unicode glyphs and
# raises UnicodeEncodeError. This is dev tooling; reliability beats prettiness.
_GLYPH = {Status.PASS: "+", Status.WARN: "!", Status.FAIL: "x"}


def _install_sink(settings) -> EventSink:
    """One-liner used by every worker-side CLI command: open the events.db sink,
    install it as the process-global sink (so ``@stage`` writes through it),
    and attach the opt-in mirror policy. The policy is a no-op when telemetry
    is disabled, so this is safe to call unconditionally.
    """
    sink = configure_sink(EventSink(settings.events_db_path))
    attach_mirror_from_settings(sink, settings)
    return sink


@click.group()
@click.version_option(__version__, prog_name="av3")
def cli() -> None:
    """Auto Applier v3 - staged, observable job-application pipeline."""


@cli.command("init-db")
def init_db() -> None:
    """Create the data dir and initialize app.db + events.db (idempotent)."""
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    conn = init_app_db(settings.app_db_path)
    conn.close()
    EventSink(settings.events_db_path).close()
    click.echo(f"Initialized v3 data dir at {settings.data_dir}")
    click.echo(f"  app.db    -> {settings.app_db_path}")
    click.echo(f"  events.db -> {settings.events_db_path}")


@cli.command()
def doctor() -> None:
    """Run preflight checks. Exits non-zero on any FAIL (CI-gateable)."""
    results = run_doctor()
    for r in results:
        line = f"  {_GLYPH[r.status]} {r.status.value:4} {r.name}: {r.detail}"
        click.echo(line)
        if r.status is not Status.PASS and r.fix:
            click.echo(f"        fix -> {r.fix}")
    fails = fail_count(results)
    warns = sum(1 for r in results if r.status is Status.WARN)
    click.echo(f"\n{len(results)} checks - {fails} fail, {warns} warn")
    sys.exit(1 if fails else 0)


# Curated seed tokens per ATS (research/ats-discovery-seeding.md), confirmed live where
# noted. Dead tokens are skipped at discovery, so generous lists are safe.
# Confirmed live 2026-05-26 (confirm-probe sweep). Dead tokens are skipped at discovery.
_GH_SEED = ["anthropic", "cloudflare", "tripadvisor", "figma", "discord", "reddit", "gitlab", "robinhood"]
_LEVER_SEED = ["matchgroup", "highspot"]
_ASHBY_SEED = ["Ashby", "Linear", "Ramp", "Vanta", "Notion", "OpenAI"]


@cli.command()
@click.option("--gh", "gh_tokens", default=",".join(_GH_SEED), help="Greenhouse board tokens.")
@click.option("--lever", "lever_sites", default=",".join(_LEVER_SEED), help="Lever site names.")
@click.option("--ashby", "ashby_slugs", default=",".join(_ASHBY_SEED), help="Ashby board slugs.")
@click.option("--max", "max_jobs", default=1, help="Jobs to inspect per token.")
def survey(gh_tokens: str, lever_sites: str, ashby_slugs: str, max_jobs: int) -> None:
    """Multi-ATS CAPTCHA-presence survey: load real Greenhouse/Lever/Ashby apply forms
    (dry-run, NEVER submit), classify the anti-bot challenge, report per-source. Opens a
    headed browser.

    Measures CAPTCHA *prevalence* (the ceiling) to compare auto-apply viability across
    ATSes — NOT the auto-pass rate, which needs real submits (a separate, gated run)."""
    import asyncio
    import json as _json
    from dataclasses import asdict
    from datetime import datetime, timezone

    from auto_applier.sources.browser.survey import (
        ashby_targets,
        gh_targets,
        lever_targets,
        run_multi_survey,
        summarize_survey,
    )
    from auto_applier.telemetry import EventSink, configure_sink

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _install_sink(settings)

    def _split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    click.echo("Building targets (confirm-probing tokens via public APIs)...")
    targets = (
        gh_targets(_split(gh_tokens), max_jobs)
        + lever_targets(_split(lever_sites), max_jobs)
        + ashby_targets(_split(ashby_slugs), max_jobs)
    )
    click.echo(f"Surveying {len(targets)} live apply forms (dry-run, no submits)...")
    rows = asyncio.run(run_multi_survey(targets, settings.browser_profile_dir))

    # Persist BEFORE printing so a display issue can never lose the measurement.
    summary = summarize_survey(rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = settings.data_dir / f"survey_{ts}.json"
    out_path.write_text(
        _json.dumps({"rows": [asdict(r) for r in rows], "summary": summary}, indent=2),
        encoding="utf-8",
    )

    for r in rows:
        if not r.form_present:
            flag = "no-form"
        elif r.captcha_type == "none":
            flag = "none"
        elif r.enterprise:
            flag = "ENTERPRISE"
        elif r.is_invisible:
            flag = "invisible"
        else:
            flag = "VISIBLE"
        click.echo(f"  {r.source:11} {r.token:14} {r.captcha_type:22} [{flag:10}] {r.title[:36]}")
    click.echo("\nSummary:")
    click.echo(_json.dumps(summary, indent=2))
    click.echo(f"\nSaved -> {out_path}")


@cli.command()
@click.option("--gh", "gh_tokens", default=None,
              help="Greenhouse board tokens (comma-sep). Overrides settings.targeting.greenhouse_boards.")
@click.option("--lever", "lever_sites", default=None,
              help="Lever site names (comma-sep). Overrides settings.targeting.lever_boards.")
@click.option("--ashby", "ashby_slugs", default=None,
              help="Ashby board slugs (comma-sep). Overrides settings.targeting.ashby_boards.")
@click.option("--title-contains", "title_contains", default=None,
              help="Comma-sep title phrases to keep (substring match). "
                   "Overrides settings.targeting.titles. Empty = no title filter.")
@click.option("--limit", type=int, default=None,
              help="Cap matched listings per board (keeps a sweep bounded).")
@click.option("--no-describe", is_flag=True, default=False,
              help="Skip the per-job Greenhouse JD fetch (faster; leaves JD empty so "
                   "the score stage will REVIEW those jobs).")
def discover(gh_tokens: str | None, lever_sites: str | None, ashby_slugs: str | None,
             title_contains: str | None, limit: int | None, no_describe: bool) -> None:
    """Discover jobs from Greenhouse/Lever/Ashby public APIs into app.db (DISCOVERED).

    The HEAD of the pipeline (spec §7 #1): sweeps each configured board token, applies
    the title filter, fills the JD (Greenhouse), dedups, and seeds DISCOVERED jobs the
    filter/score/optimize/apply stages then drain. Read-only against the ATS — no login,
    no browser, no submits. Idempotent: re-running never double-inserts.

    Boards + title filter default from settings.targeting; the flags override per run.
    """
    import asyncio

    from auto_applier.pipeline import BoardSpec, DiscoverWorker, boards_from_settings

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    def _split(s: str | None) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()] if s else []

    # Build the board list: any provided flag replaces that ATS's settings list;
    # if NO flag is given at all, fall back entirely to settings.targeting.
    if gh_tokens is None and lever_sites is None and ashby_slugs is None:
        boards = boards_from_settings(settings)
    else:
        boards = (
            [BoardSpec("greenhouse", t) for t in _split(gh_tokens)]
            + [BoardSpec("lever", t) for t in _split(lever_sites)]
            + [BoardSpec("ashby", t) for t in _split(ashby_slugs)]
        )

    title_filter = (
        _split(title_contains) if title_contains is not None
        else list(settings.targeting.titles)
    )

    worker = DiscoverWorker(
        settings=settings,
        conn=conn,
        boards=boards,
        title_filter=title_filter,
        per_board_limit=limit,
        describe_greenhouse=not no_describe,
    )

    filt = f"titles={title_filter}" if title_filter else "titles=ANY"
    click.echo(f"Discovering across {len(boards)} board(s) [{filt}]...")
    try:
        summary = asyncio.run(worker.run_once())
    finally:
        conn.close()

    click.echo(
        f"boards={summary.boards_swept} seen={summary.seen} matched={summary.matched} "
        f"new={summary.inserted} dup={summary.duplicates} described={summary.described} "
        f"errors={summary.board_errors} elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.per_source:
        for ats, n in sorted(summary.per_source.items()):
            click.echo(f"  + {ats:11} {n} new")
    for note in summary.notes:
        click.echo(f"  ! {note}")
    sys.exit(1 if summary.board_errors else 0)


# Compact location-priority tags for the digest line (see domain/location.py).
_LOC_TAG = {
    0: "★EU-remote", 1: "US/remote", 2: "EU-onsite", 3: "remote-oth", 4: "onsite-oth",
}


@cli.command()
@click.option("--limit", type=int, default=30, show_default=True,
              help="How many top jobs to show (applied AFTER filtering).")
@click.option("--min-score", type=float, default=0.0, show_default=True,
              help="Hide jobs scoring below this (0-10 scale).")
@click.option("--location", "location_mode",
              type=click.Choice(["all", "targets", "remote", "eu"]), default="all",
              show_default=True,
              help="Location filter: all | targets (remote-US/global + target-EU) | "
                   "remote (any remote) | eu (target-EU only).")
@click.option("--by-location", is_flag=True, default=False,
              help="Rank by location fit first (target-EU remote on top), then score.")
@click.option("--dimensions", "show_dims", is_flag=True, default=False,
              help="Also print the per-axis dimension breakdown.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit JSON instead of the table.")
def digest(limit: int, min_score: float, location_mode: str, by_location: bool,
           show_dims: bool, as_json: bool) -> None:
    """Ranked shortlist of scored jobs: score · location-fit · company · title · JD link.

    The read-side view for a discovery + scoring workflow — turns the scored DB into an
    actionable list. Run after discover -> filter -> score. Scores are a 0-10 first-pass
    sorter (JD vs your master profile), not gospel; eyeball the top of the list. The
    --location filter / --by-location sort apply a deterministic geography preference on
    top of the LLM fit score (target-EU-remote is the jackpot; far-flung on-site sinks).
    """
    import json as _json

    from auto_applier.domain.location import classify_location, passes_filter

    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    try:
        # Fetch all above min, then classify/filter/sort/limit in Python so the
        # location filter composes cleanly with the score ranking.
        rows = ScoreRepo(conn).list_ranked(limit=None, min_total=min_score)
    finally:
        conn.close()

    # Attach location fit; filter by the chosen mode.
    enriched = []
    for r in rows:
        fit = classify_location(r.get("location"))
        if passes_filter(fit, location_mode):
            r = {**r, "_loc_priority": fit.priority, "_loc_label": fit.label}
            enriched.append(r)

    # Re-rank if asked: location fit first (lower priority = better), then score desc.
    if by_location:
        enriched.sort(key=lambda r: (r["_loc_priority"], -r["total"]))

    enriched = enriched[:limit]

    if as_json:
        click.echo(_json.dumps(enriched, indent=2))
        return
    if not enriched:
        if not rows:
            click.echo("No scored jobs yet. Run: av3 discover -> av3 filter --once -> av3 score --once")
        else:
            click.echo(f"No jobs match --location {location_mode}. Try --location all.")
        return

    sort_desc = "location then score" if by_location else "score"
    click.echo(
        f"Top {len(enriched)} jobs (sorted by {sort_desc}; "
        f"location={location_mode}, min={min_score:g}):\n"
    )
    for i, r in enumerate(enriched, 1):
        tag = _LOC_TAG.get(r["_loc_priority"], "?")
        title = (r["title"] or "")[:42]
        company = (r["company"] or "")[:14]
        loc = (r["location"] or "")[:24]
        click.echo(f"  {i:2}. {r['total']:4.1f} {tag:10} {company:14} {title:42}  {loc}")
        click.echo(f"          {r['url']}")
        if show_dims and r.get("dimensions_json"):
            try:
                dims = _json.loads(r["dimensions_json"])
                pretty = "  ".join(f"{k}:{v:g}" for k, v in dims.items())
                click.echo(f"          {pretty}")
            except (ValueError, TypeError):
                pass


def _resolve_job_ids(settings, job_ids, shortlist_name, mark_all):
    """Resolve the target id set for `applied`/`pass`: positional ids plus, if
    --shortlist NAME --all, every job_id in that saved shortlist JSON. Returns
    (ordered_unique_ids, error_message_or_None)."""
    import json as _json

    ids: list[str] = list(job_ids)
    if shortlist_name:
        path = settings.shortlist_dir / f"{shortlist_name}.json"
        if not path.exists():
            return [], f"no saved shortlist '{shortlist_name}' at {path}"
        try:
            items = _json.loads(path.read_text(encoding="utf-8"))
            sl_ids = [it["job_id"] for it in items]
        except (ValueError, KeyError, TypeError) as exc:
            return [], f"shortlist {path} is unreadable: {exc}"
        if mark_all:
            ids.extend(sl_ids)
        elif not ids:
            return [], (f"shortlist '{shortlist_name}' has {len(sl_ids)} jobs — pass --all "
                        f"to mark them all, or name specific job ids")
    # de-dupe, preserve order
    seen: set[str] = set()
    out = [i for i in ids if not (i in seen or seen.add(i))]
    return out, None


@cli.command()
@click.option("--family", type=click.Choice([f.value for f in JobFamily]), default=None,
              help="Restrict to one role family (maps 1:1 to a résumé variant).")
@click.option("--location", "location_mode",
              type=click.Choice(["all", "targets", "remote", "eu"]), default="all",
              show_default=True, help="Location filter (same modes as `av3 digest`).")
@click.option("--limit", type=int, default=20, show_default=True,
              help="Max jobs in the shortlist.")
@click.option("--min-score", type=float, default=0.0, show_default=True,
              help="Hide jobs below this score.")
@click.option("--name", default=None,
              help="Shortlist name (file stem). Defaults to the family, else 'shortlist'.")
def shortlist(family, location_mode, limit, min_score, name) -> None:
    """Save a persistent, apply-ready shortlist (.md + .json) of un-applied jobs.

    The manual-mode read view: only DECIDED jobs (already-APPLIED / SKIPPED jobs are
    excluded, so a job you mark applied stops appearing). Ranked location-fit first
    (remote on top), then score. `av3 applied --shortlist NAME --all` then marks the
    whole batch once you've applied.
    """
    import json as _json

    from auto_applier.domain.job_family import FAMILY_LABELS, JobFamily as _JF, classify_family
    from auto_applier.domain.location import classify_location, passes_filter

    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    try:
        rows = ScoreRepo(conn).list_ranked(limit=None, min_total=min_score)
    finally:
        conn.close()

    # CRITICAL: list_ranked is NOT state-filtered — keep only DECIDED (un-applied) jobs,
    # or marked-applied jobs would re-surface, defeating the whole feature.
    rows = [r for r in rows if r.get("state") == JobState.DECIDED.value]

    enriched = []
    for r in rows:
        fam = classify_family(r.get("title"))
        if family and fam.value != family:
            continue
        fit = classify_location(r.get("location"))
        if not passes_filter(fit, location_mode):
            continue
        enriched.append({
            "job_id": r["job_id"], "score": r["total"], "title": r["title"],
            "company": r["company"], "location": r["location"] or "", "url": r["url"] or "",
            "fit": fit.label, "_p": fit.priority, "family": fam.value,
        })
    enriched.sort(key=lambda r: (r["_p"], -r["score"]))
    enriched = enriched[: limit]
    for rank, r in enumerate(enriched, 1):
        r["rank"] = rank

    if not enriched:
        click.echo(f"No DECIDED jobs match (family={family or 'any'}, location={location_mode}, "
                   f"min={min_score:g}). Try widening the filters or run discover/score first.")
        return

    fam_label = FAMILY_LABELS[_JF(family)] if family else "All families"
    stem = name or family or "shortlist"
    settings.shortlist_dir.mkdir(parents=True, exist_ok=True)
    json_path = settings.shortlist_dir / f"{stem}.json"
    md_path = settings.shortlist_dir / f"{stem}.md"

    json_path.write_text(_json.dumps(
        [{k: r[k] for k in ("rank", "job_id", "score", "title", "company", "location", "url", "fit", "family")}
         for r in enriched], indent=1), encoding="utf-8")

    md = [f"# Apply shortlist — {stem}", "",
          f"> {fam_label} · location={location_mode} · {len(enriched)} jobs · ranked by fit then score.",
          f"> Mark the whole batch applied with: `av3 applied --shortlist {stem} --all`", "",
          "| # | Score | Fit | Title | Company | Apply | job_id |",
          "|---|---|---|---|---|---|---|"]
    for r in enriched:
        link = f"[link]({r['url']})" if r["url"] else "—"
        md.append(f"| {r['rank']} | {r['score']:.1f} | {r['fit']} | {r['title']} | "
                  f"{r['company']} | {link} | `{r['job_id']}` |")
    md.append("")
    md_path.write_text("\n".join(md), encoding="utf-8")

    click.echo(f"Wrote {len(enriched)} jobs -> {md_path}")
    click.echo(f"             and -> {json_path}")
    click.echo(f"Mark all applied after you apply:  av3 applied --shortlist {stem} --all")


@cli.command()
@click.argument("job_ids", nargs=-1)
@click.option("--shortlist", "shortlist_name", default=None,
              help="Name of a saved shortlist (file stem in the shortlist dir).")
@click.option("--all", "mark_all", is_flag=True, default=False,
              help="With --shortlist: mark EVERY job in that shortlist applied.")
@click.option("--resume", "resume_path", default="",
              help="Optional path to the résumé variant you applied with (recorded).")
def applied(job_ids, shortlist_name, mark_all, resume_path) -> None:
    """Record that you applied to one or more jobs externally (manual mode → APPLIED).

    Pass job ids and/or --shortlist NAME (with --all to mark the whole saved shortlist).
    Marked jobs leave the DECIDED pool: they won't appear in future shortlists/digests and
    are deduped out of future discovery. Idempotent and batch-safe.
    """
    from auto_applier.pipeline.manual_apply import mark_manually_applied

    settings = load_settings()
    ids, err = _resolve_job_ids(settings, job_ids, shortlist_name, mark_all)
    if err:
        click.echo(f"  x FAIL {err}", err=True)
        sys.exit(2)
    if not ids:
        click.echo("  x FAIL no job ids (pass ids or --shortlist NAME --all)", err=True)
        sys.exit(2)

    applied_n = already_n = error_n = 0
    conn = init_app_db(settings.app_db_path)
    try:
        for jid in ids:
            res = mark_manually_applied(conn, jid, resume_path=resume_path)
            if res.status == "applied":
                applied_n += 1
                click.echo(f"  + applied  {jid}  {res.detail}")
            elif res.status == "already":
                already_n += 1
                click.echo(f"  - already  {jid}  {res.detail}")
            else:
                error_n += 1
                click.echo(f"  x error    {jid}  {res.detail}", err=True)
    finally:
        conn.close()
    click.echo(f"\napplied={applied_n} already={already_n} errors={error_n}")
    if error_n:
        sys.exit(1)


@cli.command("pass")
@click.argument("job_ids", nargs=-1)
@click.option("--shortlist", "shortlist_name", default=None,
              help="Name of a saved shortlist (file stem in the shortlist dir).")
@click.option("--all", "mark_all", is_flag=True, default=False,
              help="With --shortlist: pass on EVERY job in that shortlist.")
def pass_cmd(job_ids, shortlist_name, mark_all) -> None:
    """Pass on jobs you looked at but won't apply to (DECIDED → SKIPPED).

    Stops them surfacing in shortlists/digests. (SKIPPED is ephemeral — eligible for
    pruning after the retention window — so a passed job may eventually re-surface if
    re-discovered; that's intended.)
    """
    from auto_applier.db.engine import tx
    from auto_applier.domain.state import InvalidTransition

    settings = load_settings()
    ids, err = _resolve_job_ids(settings, job_ids, shortlist_name, mark_all)
    if err:
        click.echo(f"  x FAIL {err}", err=True)
        sys.exit(2)
    if not ids:
        click.echo("  x FAIL no job ids (pass ids or --shortlist NAME --all)", err=True)
        sys.exit(2)

    passed_n = error_n = 0
    conn = init_app_db(settings.app_db_path)
    try:
        repo = JobRepo(conn)
        for jid in ids:
            job = repo.get(jid)
            if job is None:
                error_n += 1
                click.echo(f"  x error  {jid}  not found", err=True)
                continue
            if job.state is JobState.SKIPPED:
                click.echo(f"  - already {jid}  already SKIPPED")
                continue
            try:
                with tx(conn):
                    repo.set_state(jid, JobState.SKIPPED)
                passed_n += 1
                click.echo(f"  + passed  {jid}  {job.company} — {job.title}")
            except (InvalidTransition, KeyError) as exc:
                error_n += 1
                click.echo(f"  x error  {jid}  {exc}", err=True)
    finally:
        conn.close()
    click.echo(f"\npassed={passed_n} errors={error_n}")
    if error_n:
        sys.exit(1)


@cli.command()
def status() -> None:
    """Show job counts by state from app.db."""
    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    try:
        counts = JobRepo(conn).count_by_state()
    finally:
        conn.close()
    if not counts:
        click.echo("No jobs yet. Run `av3 discover` to seed jobs.")
        return
    click.echo("Jobs by state:")
    for state, n in sorted(counts.items()):
        click.echo(f"  {state:14} {n}")


@cli.command("learn")
@click.option("--top", type=int, default=15, show_default=True,
              help="Show the top N skill gaps.")
@click.option("--min-demand", type=int, default=1, show_default=True,
              help="Only show skills demanded by at least this many JDs.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the trend list as JSON instead of the ASCII table.")
def learn_cmd(top: int, min_demand: int, as_json: bool) -> None:
    """What to learn next (spec section 10 skill-gap trends): rank skills your fact bank
    lacks by demand from your HIGH-FIT jobs (score >= 7).

    Read-only. Combines stored JDs + their scores: a skill demanded by jobs you already
    score well on is the highest-leverage thing to learn. Pair with `av3 reconcile --apply`
    once you actually have a skill.
    """
    import json as _json

    from auto_applier.analytics import compute_skill_gap_trends
    from auto_applier.db.repositories import ScoreRepo
    from auto_applier.resume.factbank import FactBank

    settings = load_settings()
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo("        fix -> seed the fact bank during onboarding first.", err=True)
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    try:
        jobs = JobRepo(conn).list_all_with_description()
        scores = ScoreRepo(conn).totals_by_job()
    finally:
        conn.close()

    trends = [
        t for t in compute_skill_gap_trends(jobs, scores, bank.skills, top=None)
        if t.demand_count >= min_demand
    ][:top]

    if as_json:
        click.echo(_json.dumps([vars(t) for t in trends], indent=2, default=str))
        return

    if not trends:
        click.echo(
            "No skill-gap trends yet. Need stored jobs with descriptions (run discovery) "
            "— and scores (`av3 score`) sharpen the high-fit ranking."
        )
        return
    click.echo(
        f"What to learn next ({len(jobs)} job(s) scanned; ranked by high-fit demand):"
    )
    for t in trends:
        avg = f"{t.avg_demanding_score:.1f}" if t.avg_demanding_score is not None else "  -"
        click.echo(
            f"  {t.high_fit_count:3} high-fit / {t.demand_count:3} total  "
            f"avg-score={avg}  {t.skill}"
        )
    click.echo(
        "\nHave one of these? Add it:  av3 reconcile --apply \"Skill Name\""
    )


@cli.command("reconcile")
@click.option("--scan", is_flag=True, default=False,
              help="Scan every stored job's JD and record demanded-but-missing skills as "
                   "gaps BEFORE showing proposals (the §7b surfacing step). Gather-only — "
                   "writes the gap table, never the fact bank.")
@click.option("--min-count", type=int, default=1, show_default=True,
              help="Only propose skills demanded by at least this many JDs (recurrence).")
@click.option("--apply", "apply_skills", default="",
              help="Comma-separated skills to INSERT into the fact bank (the gated act). "
                   "Additive — appends to master.json and marks those gaps reconciled. "
                   "Omit to preview only.")
def reconcile_cmd(scan: bool, min_count: int, apply_skills: str) -> None:
    """Batch skill-reconciliation (spec section 7b): surface skills the stored JDs demand
    but your fact bank lacks, and (with --apply) insert approved ones into the bank.

    Default is PREVIEW (read-only). --scan records gaps from JD text first. --apply is the
    only path that mutates the fact bank, and it's additive (never wipes existing skills) —
    keeping the fabrication-guard source of truth under explicit user control.
    """
    from auto_applier.db import SkillGapRepo
    from auto_applier.reconcile import apply_proposals, build_proposals, record_batch_gaps
    from auto_applier.resume.factbank import FactBank
    from auto_applier.web.onboarding import save_fact_bank

    settings = load_settings()
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo("        fix -> seed the fact bank during onboarding first.", err=True)
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    try:
        gap_repo = SkillGapRepo(conn)

        if scan:
            jobs = JobRepo(conn).list_all_with_description()
            bumps = record_batch_gaps(jobs, bank, gap_repo)
            conn.commit()
            click.echo(f"scanned {len(jobs)} job(s); recorded {bumps} skill-gap bump(s).")

        if apply_skills.strip():
            approved = [s.strip() for s in apply_skills.split(",") if s.strip()]
            before = len(bank.skills)
            apply_proposals(bank, approved)
            added = len(bank.skills) - before
            save_fact_bank(settings.data_dir, bank)
            for skill in approved:
                gap_repo.set_status(skill, "certified")
            conn.commit()
            click.echo(
                f"applied {added} new skill(s) to the fact bank "
                f"({len(bank.skills)} total); marked {len(approved)} gap(s) reconciled."
            )
            return

        proposals = build_proposals(bank, gap_repo, min_count=min_count)
    finally:
        conn.close()

    if not proposals:
        click.echo(
            "No skill-gap proposals. Run `av3 reconcile --scan` to surface skills from "
            "stored JDs, or lower --min-count."
        )
        return
    click.echo(f"Skill-gap proposals (>= {min_count} JD(s) demand, not in bank):")
    for p in proposals:
        click.echo(f"  {p.count:4}x  {p.skill}")
    click.echo(
        "\nReview, then insert the ones you actually have:\n"
        "  av3 reconcile --apply \"Skill One,Skill Two\""
    )


@cli.command("outcome")
@click.argument("job_id")
@click.argument("kind", type=click.Choice(
    ["response", "interview", "offer", "rejection", "ghost"]))
@click.option("--note", default="", help="Optional free-text note (no PII needed).")
def outcome_cmd(job_id: str, kind: str, note: str) -> None:
    """Record a post-apply outcome for a job (spec section 8e outcome feedback loop).

    KIND is one of response | interview | offer | rejection | ghost. A job can accrue
    several outcomes over time; analytics derives the furthest-reached stage. Only
    meaningful for APPLIED jobs (the command warns otherwise but still records).
    """
    from auto_applier.db import OutcomeRepo
    from auto_applier.domain.models import Outcome
    from auto_applier.domain.state import JobState, OutcomeKind

    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    try:
        job = JobRepo(conn).get(job_id)
        if job is None:
            click.echo(f"  x FAIL no job with id {job_id}", err=True)
            sys.exit(2)
        if job.state is not JobState.APPLIED:
            click.echo(
                f"  ! warning: job {job_id} is {job.state.value}, not APPLIED — "
                f"outcome recorded but it won't appear in conversion analytics "
                f"(which only counts APPLIED jobs)."
            )
        rec = OutcomeRepo(conn).add(Outcome(job_id=job_id, kind=OutcomeKind(kind), note=note))
        conn.commit()
    finally:
        conn.close()
    click.echo(f"recorded outcome={rec.kind.value} job={job_id} at={rec.noted_at}")


@cli.command("analytics")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the conversion report as JSON instead of the ASCII tables.")
@click.option("--min-samples", type=int, default=None,
              help="Override the minimum APPLIED-job count before weight nudges are "
                   "suggested (default 20). Below it, no nudge — don't tune on noise.")
def analytics_cmd(as_json: bool, min_samples: int | None) -> None:
    """Show outcome-feedback analytics (spec section 8e): conversion rates by source,
    title, and score-band, plus any *suggested* scoring-weight nudges.

    Read-only. Weight nudges are RECOMMENDATIONS — applying them is a deliberate user
    edit to user_config.json, never an auto-mutation (tuning live scoring off sparse
    early data is the anti-pattern this avoids).
    """
    import json as _json

    from auto_applier.analytics import (
        MIN_SAMPLES_FOR_NUDGE,
        compute_conversion_report,
        recommend_weight_nudges,
    )
    from auto_applier.db import OutcomeRepo

    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    try:
        feed = OutcomeRepo(conn).applied_with_outcomes()
    finally:
        conn.close()

    report = compute_conversion_report(feed)
    nudges = recommend_weight_nudges(
        report,
        min_samples=min_samples if min_samples is not None else MIN_SAMPLES_FOR_NUDGE,
    )

    if as_json:
        click.echo(_json.dumps({
            "total_applied": report.total_applied,
            "total_converted": report.total_converted,
            "overall_rate": round(report.overall_rate, 4),
            "outcome_counts": report.outcome_counts,
            "by_source": [vars(s) | {"rate": round(s.rate, 4)} for s in report.by_source],
            "by_title": [vars(s) | {"rate": round(s.rate, 4)} for s in report.by_title],
            "by_band": [vars(s) | {"rate": round(s.rate, 4)} for s in report.by_band],
            "nudges": [vars(n) for n in nudges],
        }, indent=2, default=str))
        return

    if report.total_applied == 0:
        click.echo("No APPLIED jobs yet — nothing to analyze. Record outcomes with "
                   "`av3 outcome <job_id> <kind>` after you apply.")
        return

    click.echo(
        f"Applied={report.total_applied} converted={report.total_converted} "
        f"rate={report.overall_rate:.0%}"
    )
    if report.outcome_counts:
        parts = " ".join(f"{k}={v}" for k, v in sorted(report.outcome_counts.items()))
        click.echo(f"Outcomes: {parts}")

    def _table(title: str, stats) -> None:
        click.echo(f"\n{title}:")
        if not stats:
            click.echo("  (none)")
            return
        for s in stats[:15]:
            click.echo(
                f"  {s.key[:32]:32} applied={s.applied:4} conv={s.converted:4} "
                f"ghost={s.ghosted:4} rate={s.rate:.0%}"
            )

    _table("By source", report.by_source)
    _table("By score-band", report.by_band)
    _table("By title", report.by_title)

    click.echo("\nSuggested weight nudges (advisory — edit user_config.json to apply):")
    if not nudges:
        click.echo("  (none — need more data or no material signal yet)")
    else:
        for n in nudges:
            arrow = "+" if n.direction > 0 else "-"
            click.echo(f"  {arrow}{n.axis}: {n.rationale}")


@cli.group("stories")
def stories_group() -> None:
    """STAR+R interview story bank (spec section 11 extras — on-demand prep).

    Stories are generated from the master fact bank (the fabrication-guard source
    of truth) tailored to one job, and accumulate in story_bank.json into a
    reusable library that can answer any behavioral interview question.
    """


@stories_group.command("generate")
@click.argument("job_id")
def stories_generate(job_id: str) -> None:
    """Generate 3 STAR+R stories tailored to JOB_ID and append them to the bank.

    Needs the job's stored description and a reachable Ollama. Stories use only
    fact-bank facts; read them back before an interview — they're prep notes,
    you own the words.
    """
    import asyncio

    from auto_applier.llm.complete import build_default
    from auto_applier.resume.factbank import FactBank
    from auto_applier.resume.story_bank import StoryGenerator, append_stories

    settings = load_settings()
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo("        fix -> seed the fact bank during onboarding first.", err=True)
        sys.exit(2)
    bank = FactBank.load(fact_bank_path)

    conn = init_app_db(settings.app_db_path)
    try:
        job = JobRepo(conn).get(job_id)
    finally:
        conn.close()
    if job is None:
        click.echo(f"  x FAIL no job with id {job_id}", err=True)
        sys.exit(2)
    if not job.description.strip():
        click.echo(f"  x FAIL job {job_id} has no stored description to tailor against.", err=True)
        sys.exit(2)

    generator = StoryGenerator(build_default(settings))
    stories = asyncio.run(generator.generate(
        bank, job.description, company=job.company, title=job.title, job_id=job.id,
    ))
    if not stories:
        click.echo(
            "  x no stories generated (LLM unreachable or reply malformed) — "
            "check `av3 doctor` and retry.", err=True,
        )
        sys.exit(1)
    append_stories(settings.story_bank_path, stories)
    click.echo(f"added {len(stories)} stories for {job.title} @ {job.company}:")
    for s in stories:
        click.echo(f"  - {s.title}" + (f"  (answers: {s.question_prompt})" if s.question_prompt else ""))
    click.echo(f"bank: {settings.story_bank_path}  (export: av3 stories export)")


@stories_group.command("list")
def stories_list() -> None:
    """List the stories in the bank (title + provenance)."""
    from auto_applier.resume.story_bank import load_bank

    settings = load_settings()
    stories = load_bank(settings.story_bank_path)
    if not stories:
        click.echo("Story bank is empty. Generate with: av3 stories generate <job_id>")
        return
    click.echo(f"{len(stories)} stories in {settings.story_bank_path}:")
    for i, s in enumerate(stories, 1):
        origin = f"  ({s.job_title} @ {s.company})" if (s.company or s.job_title) else ""
        click.echo(f"  {i:3}. {s.title}{origin}")


@stories_group.command("export")
@click.option("--out", "out_path", default=None,
              help="Markdown output path (default: story_bank.md beside the JSON bank).")
def stories_export(out_path: str | None) -> None:
    """Export the whole bank as a readable markdown prep document."""
    from pathlib import Path

    from auto_applier.resume.story_bank import export_bank_markdown, load_bank

    settings = load_settings()
    stories = load_bank(settings.story_bank_path)
    target = Path(out_path) if out_path else settings.story_bank_path.with_suffix(".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(export_bank_markdown(stories), encoding="utf-8")
    click.echo(f"wrote {len(stories)} stories -> {target}")


@cli.command("research")
@click.argument("company")
@click.option("--source-file", "source_file", default=None,
              help="File holding the pasted source material (career page text, articles, "
                   "notes). Omit to read it from stdin (paste, then Ctrl-Z/Ctrl-D).")
@click.option("--show", is_flag=True, default=False,
              help="Print the previously saved briefing for COMPANY and exit (no LLM).")
def research_cmd(company: str, source_file: str | None, show: bool) -> None:
    """Build a grounded interview-prep briefing for COMPANY (spec section 11 extras).

    You paste the source material (the tool never fetches anything — zero egress);
    the local LLM distills it into what-they-do / tech-stack / culture / red-flags /
    questions-to-ask, saying "not in source" instead of guessing. Saved as md + json
    under the data dir's research/ folder.
    """
    import asyncio

    from auto_applier.llm.complete import build_default
    from auto_applier.research import (
        CompanyResearcher,
        briefing_path,
        load_briefing,
        save_briefing,
    )

    settings = load_settings()

    if show:
        briefing = load_briefing(settings.research_dir, company)
        if briefing is None:
            click.echo(f"no saved briefing for {company!r} "
                       f"(expected at {briefing_path(settings.research_dir, company)})", err=True)
            sys.exit(2)
        click.echo(briefing.to_markdown())
        return

    if source_file:
        from pathlib import Path

        src = Path(source_file)
        if not src.exists():
            click.echo(f"  x FAIL source file not found: {src}", err=True)
            sys.exit(2)
        material = src.read_text(encoding="utf-8", errors="replace")
    else:
        click.echo("Paste the source material, then end input (Ctrl-Z then Enter on "
                   "Windows, Ctrl-D elsewhere):", err=True)
        material = sys.stdin.read()

    if not material.strip():
        click.echo("  x FAIL no source material provided — refusing to invent a briefing.", err=True)
        sys.exit(2)

    researcher = CompanyResearcher(build_default(settings))
    briefing = asyncio.run(researcher.research(company, material))
    if briefing is None:
        click.echo(
            "  x no briefing produced (LLM unreachable, reply malformed, or the source "
            "had nothing grounded) — check `av3 doctor` and retry.", err=True,
        )
        sys.exit(1)
    md_path = save_briefing(settings.research_dir, briefing)
    click.echo(f"wrote briefing -> {md_path}")
    click.echo(briefing.to_markdown())


@cli.command()
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum QUEUED_APPLY jobs to process this run.")
@click.option("--source", type=click.Choice(["lever", "greenhouse", "ashby"]), default=None,
              help="Process only this source's jobs (subsets the driver registry).")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Dev-safe default. --no-dry-run SENDS REAL APPLICATIONS.")
@click.option("--mode", type=click.Choice(["auto", "assisted"]), default="auto",
              help="auto = bot fills + submits on clean forms; assisted = pre-fill, human submits.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama wiring. Resolver uses bank + sensitive policy only.")
def apply(once: bool, limit: int | None, source: str | None,
          dry_run: bool, mode: str, no_llm: bool) -> None:
    """Drain QUEUED_APPLY jobs through the apply worker (spec section 7 #7).

    Constructs the resolver from the fact bank, opens one stealthy Chrome session, and
    walks each queued job through the per-ATS driver. --dry-run keeps the job in
    QUEUED_APPLY (no state ping-pong); --no-dry-run is the gated path that actually
    submits.
    """
    import asyncio

    from auto_applier.domain.state import ApplyMode
    from auto_applier.llm.complete import build_default
    from auto_applier.llm.embed import OllamaEmbeddings
    from auto_applier.pipeline import ApplyWorker, default_drivers
    from auto_applier.resume.factbank import FactBank
    from auto_applier.sources.browser.session import BrowserSession
    from auto_applier.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank + resume file MUST exist. Clearer to fail here with a
    # doctor-style fix hint than to crash mid-run.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    resume_path = settings.artifacts_dir / "resume.pdf"
    if not resume_path.exists():
        click.echo(f"  x FAIL resume: missing at {resume_path}", err=True)
        click.echo(
            "        fix -> drop a resume.pdf into the artifacts dir until "
            "section 6b resume generation lands.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    drivers = default_drivers()
    if source:
        drivers = {source: drivers[source]}

    # Embed + LLM clients are HTTP-lazy: constructor doesn't touch the network, so a
    # down Ollama just surfaces at resolve time and the resolver falls through to REVIEW.
    embed = None if no_llm else OllamaEmbeddings(
        host=settings.llm.ollama_host, model=settings.llm.embed_model
    )
    llm = None if no_llm else build_default(settings)

    apply_mode = ApplyMode.BROWSER_AUTO if mode == "auto" else ApplyMode.BROWSER_ASSISTED

    # Loud confirmation before the irreversible path. The handoff calls this out as a
    # gated user decision and the spec stays "safety floor never tunable by config".
    if not dry_run:
        click.echo(
            f"! --no-dry-run: real submits to source={source or 'lever+greenhouse'} "
            f"mode={mode} limit={limit if limit is not None else 'unbounded'}"
        )

    async def _run():
        session = BrowserSession(settings.browser_profile_dir)
        await session.start()
        try:
            worker = ApplyWorker(
                settings=settings,
                conn=conn,
                fact_bank=bank,
                resume_path=str(resume_path),
                new_page=session.new_page,
                embed_client=embed,
                llm_client=llm,
                mode=apply_mode,
                dry_run=dry_run,
                drivers=drivers,
            )
            return await worker.run_once(limit=limit)
        finally:
            await session.stop()

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; the cp1252 Windows console reconfigures at module top but
    # we still avoid unicode glyphs to match doctor/status output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"applied={summary.applied} review={summary.review} "
        f"skipped={summary.skipped} paused={summary.paused} "
        f"errors={summary.errors} recovered={summary.recovered} "
        f"deferred={summary.deferred_daily_target} rotated={summary.rotated} "
        f"dry_run={summary.dry_run_count} elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: any per-job exception is a non-zero exit.
    sys.exit(1 if summary.errors else 0)


@cli.command("filter")
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum DISCOVERED jobs to process this run.")
@click.option("--threshold", type=float, default=0.6, show_default=True,
              help="Cosine similarity threshold; >= passes to DESCRIBED, < routes to FILTERED.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama. Every DISCOVERED job fail-opens to DESCRIBED (no filtering).")
def filter_cmd(once: bool, limit: int | None, threshold: float, no_llm: bool) -> None:
    """Drain DISCOVERED jobs through the embedding pre-filter (spec section 7 #3).

    Cosine-rank (title + company + snippet) against the master fact-bank summary;
    above threshold passes to DESCRIBED for the next stage to scrape the full JD,
    below threshold routes to FILTERED (terminal). Fail-open if Ollama is unreachable
    or the bank is empty - the alternative would be silently dropping every job into a
    terminal state, which the dashboard couldn't reverse.
    """
    import asyncio

    from auto_applier.llm.embed import OllamaEmbeddings
    from auto_applier.pipeline import FilterWorker
    from auto_applier.resume.factbank import FactBank
    from auto_applier.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank MUST exist. No resume.pdf check here - the filter only
    # reads the bank (skills/titles/bullets) to form its anchor; the per-job resume
    # is built downstream in the optimize stage.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    # Embed client is HTTP-lazy: constructor doesn't touch the network, so a down
    # Ollama just surfaces at run time and the worker fail-opens per-job.
    embed = None if no_llm else OllamaEmbeddings(
        host=settings.llm.ollama_host, model=settings.llm.embed_model
    )

    async def _run():
        worker = FilterWorker(
            settings=settings,
            conn=conn,
            fact_bank=bank,
            embed_client=embed,
            threshold=threshold,
        )
        return await worker.run_once(limit=limit)

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"passed={summary.passed} filtered={summary.filtered} "
        f"failed_open={summary.failed_open} errors={summary.errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: per-job exceptions are a non-zero exit even when
    # they're fail-opened (so a misconfigured Ollama still trips a monitoring alert).
    sys.exit(1 if summary.errors else 0)


@cli.command("score")
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum DESCRIBED jobs to process this run.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama. Every DESCRIBED job will SKIP (fail-closed) "
                   "with total=0.0 - the opposite of filter --no-llm because scoring "
                   "without an LLM cannot honestly produce a merit-based pass.")
def score_cmd(once: bool, limit: int | None, no_llm: bool) -> None:
    """Drain DESCRIBED jobs through the score worker (spec section 7 #5).

    LLM dimension-scores the full JD against the master fact-bank profile across the
    seven weighted axes from settings.scoring, computes the weighted total, and
    walks the state machine: above review_min stays at DECIDED for the optimize
    worker to pick up; below review_min walks DECIDED -> SKIPPED here so the
    optimize worker can trust every DECIDED job it sees is worth optimizing.

    Fail-CLOSED: missing LLM / empty JD / per-job LLM exception all walk that job
    through SCORED -> DECIDED -> SKIPPED with total=0.0 (the opposite of filter's
    fail-open posture, because fail-open here would auto-apply unscored jobs).
    """
    import asyncio

    from auto_applier.llm.complete import build_default
    from auto_applier.pipeline import ScoreWorker
    from auto_applier.resume.factbank import FactBank
    from auto_applier.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank MUST exist - the bank summary becomes the profile side
    # of every score prompt. No resume.pdf check (scoring doesn't touch the file;
    # the optimize worker builds the per-job resume from the bank).
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    # LLM client is HTTP-lazy: constructor doesn't touch the network, so a down
    # Ollama just surfaces at run time and the worker fail-closes per-job.
    llm = None if no_llm else build_default(settings)

    if no_llm:
        click.echo(
            "! --no-llm: every DESCRIBED job will SKIP with total=0.0 "
            "(scoring requires an LLM)."
        )

    async def _run():
        worker = ScoreWorker(
            settings=settings,
            conn=conn,
            fact_bank=bank,
            llm_client=llm,
        )
        return await worker.run_once(limit=limit)

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply/filter output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"decided={summary.decided} below_bar={summary.below_bar} "
        f"failed_closed={summary.failed_closed} errors={summary.errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: per-job exceptions are a non-zero exit even when
    # fail-closed (so a misconfigured Ollama still trips a monitoring alert).
    sys.exit(1 if summary.errors else 0)


@cli.command("optimize")
@click.option("--once", is_flag=True, default=True,
              help="Run one cycle and exit. The only mode v3.0 ships (staged scheduler is Phase 3).")
@click.option("--limit", type=int, default=None,
              help="Maximum DECIDED jobs to process this run.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama. Every DECIDED job will route to REVIEW "
                   "(fail-closed) - the Strict gate cannot generate a tailored "
                   "resume without an LLM.")
def optimize_cmd(once: bool, limit: int | None, no_llm: bool) -> None:
    """Drain DECIDED jobs through the optimize+Strict gate (spec section 7 #6).

    For each above-bar DECIDED job: generate a per-job tailored resume from the
    fact bank, generate a cover letter, run the fabrication guard. ALL THREE must
    pass or the job routes to REVIEW. Pass -> QUEUED_APPLY (apply worker reads it
    blindly, so the Strict gate IS the safety mechanism that justifies
    BROWSER_AUTO - never auto-submit un-optimized).

    Fail-CLOSED: missing LLM / per-job LLM exception / guard rejection / PDF
    render failure all route that one job to REVIEW. Other jobs in the run
    continue (per-job isolation). The CLI exits 1 on errors > 0 so a misconfigured
    Ollama trips monitoring even when the per-job behavior is graceful.
    """
    import asyncio

    from auto_applier.llm.complete import build_default
    from auto_applier.pipeline import OptimizeWorker
    from auto_applier.resume.factbank import FactBank
    from auto_applier.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank MUST exist - both generation prompts read from it,
    # and the guard uses it as the truth side of the allow-list. No resume.pdf
    # check here: this command writes the per-job PDFs.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    # Ensure the artifacts dir exists - the worker's renderer mkdirs the
    # generated/ subdir, but the parent must exist for that to chain.
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    llm = None if no_llm else build_default(settings)

    if no_llm:
        click.echo(
            "! --no-llm: every DECIDED job will route to REVIEW "
            "(the Strict gate requires an LLM to generate the per-job resume)."
        )

    async def _run():
        worker = OptimizeWorker(
            settings=settings,
            conn=conn,
            fact_bank=bank,
            llm_client=llm,
        )
        return await worker.run_once(limit=limit)

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply/filter/score output style.
    click.echo(
        f"run_id={summary.run_id} attempted={summary.attempted} "
        f"queued={summary.queued} routed_to_review={summary.routed_to_review} "
        f"guard_rejected={summary.guard_rejected} render_failed={summary.render_failed} "
        f"failed_closed={summary.failed_closed} errors={summary.errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    if summary.notes:
        click.echo("Notes:")
        for note in summary.notes:
            click.echo(f"  - {note}")

    # CI / cron friendliness: per-job exceptions are a non-zero exit even when
    # fail-closed (so a misconfigured Ollama still trips a monitoring alert).
    # Guard rejections and render failures do NOT trip the exit code - those are
    # intended-pathway outcomes (the gate is supposed to reject bad output).
    sys.exit(1 if summary.errors else 0)


@cli.command("run")
@click.option("--max-cycles", type=int, default=None,
              help="Stop after N cycles. Default: run forever (Ctrl-C to stop).")
@click.option("--quiet-hours", type=str, default=None,
              help="Local-time window HH:MM-HH:MM during which the apply worker pauses "
                   "(gather stages keep running). Overrides settings.scheduler.quiet_hours.")
@click.option("--cycle-interval-s", type=float, default=None,
              help="Seconds between cycles. Overrides settings.scheduler.cycle_interval_s.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Dev-safe default. --no-dry-run lets the apply worker SEND REAL APPLICATIONS.")
@click.option("--mode", type=click.Choice(["auto", "assisted"]), default="auto",
              help="Apply mode: auto = bot submits on clean forms; assisted = pre-fill, human submits.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama wiring. Filter fail-opens, score+optimize fail-CLOSED, "
                   "apply resolver uses bank + sensitive policy only.")
@click.option("--no-discover", is_flag=True, default=False,
              help="Don't run discovery in the loop (drain only what's already in app.db). "
                   "By default the loop sweeps settings.targeting boards each cycle.")
@click.option("--discover-limit", type=int, default=None,
              help="Cap matched listings per board per cycle (bounds a discovery sweep).")
def run_cmd(max_cycles: int | None, quiet_hours: str | None,
            cycle_interval_s: float | None, dry_run: bool, mode: str,
            no_llm: bool, no_discover: bool, discover_limit: int | None) -> None:
    """Always-on staged-worker loop (spec section 7a) — THE production entry.

    Drives filter -> score -> optimize -> apply each cycle in pipeline order
    (so a freshly DISCOVERED job can flow all the way through in one cycle when
    queues are mostly idle). Apply stage is the only one gated by quiet hours;
    gather stages (filter/score/optimize) run 24/7.

    The --once mode on per-worker commands (av3 filter / score / optimize /
    apply --once) stays available for testing and doctor checks; this is what
    you run when the bot should just keep working.
    """
    import asyncio

    from auto_applier.domain.state import ApplyMode
    from auto_applier.llm.complete import build_default
    from auto_applier.llm.embed import OllamaEmbeddings
    from auto_applier.pipeline import (
        ApplyWorker,
        DiscoverWorker,
        FilterWorker,
        OptimizeWorker,
        Scheduler,
        ScoreWorker,
        default_drivers,
        parse_quiet_hours,
    )
    from auto_applier.resume.factbank import FactBank
    from auto_applier.sources.browser.session import BrowserSession
    from auto_applier.telemetry import configure_sink

    settings = load_settings()

    # Pre-flight: fact bank + resume file. The apply worker still reads a
    # single global resume.pdf as a fallback (per-job derived paths from the
    # optimize worker land alongside the apply-worker rewire in a future
    # sub-phase).
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    if not fact_bank_path.exists():
        click.echo(f"  x FAIL fact bank: missing at {fact_bank_path}", err=True)
        click.echo(
            "        fix -> seed the fact bank during onboarding (Phase 4) "
            "or drop a master.json there.",
            err=True,
        )
        sys.exit(2)

    resume_path = settings.artifacts_dir / "resume.pdf"
    if not resume_path.exists():
        click.echo(f"  x FAIL resume: missing at {resume_path}", err=True)
        click.echo(
            "        fix -> drop a resume.pdf into the artifacts dir until the "
            "apply worker reads per-job optimize-generated paths.",
            err=True,
        )
        sys.exit(2)

    bank = FactBank.load(fact_bank_path)
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Build LLM clients ONCE and share across workers — Ollama HTTP is lazy so
    # construction is free even when --no-llm is set (we just pass None instead).
    embed = None if no_llm else OllamaEmbeddings(
        host=settings.llm.ollama_host, model=settings.llm.embed_model
    )
    llm = None if no_llm else build_default(settings)

    apply_mode = ApplyMode.BROWSER_AUTO if mode == "auto" else ApplyMode.BROWSER_ASSISTED

    # CLI overrides win over settings defaults; both fall back to the spec defaults.
    effective_cycle_interval = (
        cycle_interval_s if cycle_interval_s is not None
        else settings.scheduler.cycle_interval_s
    )
    effective_quiet_hours_raw = (
        quiet_hours if quiet_hours is not None
        else settings.scheduler.quiet_hours
    )
    quiet_hours_window = parse_quiet_hours(effective_quiet_hours_raw)

    # Loud confirmation before the irreversible path.
    if not dry_run:
        click.echo(
            f"! --no-dry-run: scheduler will SEND REAL APPLICATIONS in mode={mode} "
            f"(quiet_hours={effective_quiet_hours_raw or 'none'}, "
            f"max_cycles={max_cycles if max_cycles is not None else 'unbounded'})"
        )

    # Maintenance hook: prune ephemeral + events + back up both DBs every
    # ``retention.maintenance_interval_s`` seconds. Defined as an async
    # closure so the scheduler can call it without knowing about retention
    # internals. Constructed once and threaded into the scheduler.
    from auto_applier.pipeline.retention import (
        prune_ephemeral as _prune_ephemeral,
        prune_events as _prune_events,
        run_backup_cycle as _run_backup_cycle,
    )

    async def _maintenance():
        _prune_ephemeral(conn, settings.retention.ephemeral_days)
        _prune_events(settings.events_db_path, settings.retention.events_days)
        _run_backup_cycle(settings)

    # Discovery is the head of the loop unless --no-discover. Boards + title filter
    # come from settings.targeting; built once and reused across cycles (shared
    # per-host throttle inside each source).
    discover_worker = None if no_discover else DiscoverWorker(
        settings=settings, conn=conn, per_board_limit=discover_limit,
    )

    async def _run():
        session = BrowserSession(settings.browser_profile_dir)
        await session.start()
        try:
            filter_worker = FilterWorker(
                settings=settings, conn=conn, fact_bank=bank,
                embed_client=embed,
            )
            score_worker = ScoreWorker(
                settings=settings, conn=conn, fact_bank=bank,
                llm_client=llm,
            )
            optimize_worker = OptimizeWorker(
                settings=settings, conn=conn, fact_bank=bank,
                llm_client=llm,
            )
            apply_worker = ApplyWorker(
                settings=settings, conn=conn, fact_bank=bank,
                resume_path=str(resume_path),
                new_page=session.new_page,
                embed_client=embed,
                llm_client=llm,
                mode=apply_mode,
                dry_run=dry_run,
                drivers=default_drivers(),
            )
            scheduler = Scheduler(
                discover_worker=discover_worker,
                filter_worker=filter_worker,
                score_worker=score_worker,
                optimize_worker=optimize_worker,
                apply_worker=apply_worker,
                cycle_interval_s=effective_cycle_interval,
                quiet_hours=quiet_hours_window,
                maintenance=_maintenance,
                maintenance_interval_s=settings.retention.maintenance_interval_s,
            )
            return await scheduler.run(max_cycles=max_cycles)
        finally:
            await session.stop()

    try:
        summary = asyncio.run(_run())
    finally:
        conn.close()

    # ASCII-only summary line; matches doctor/status/apply/filter/score output style.
    click.echo(
        f"cycles={len(summary.cycles)} total_errors={summary.total_errors} "
        f"elapsed={summary.elapsed_s:.1f}s"
    )
    # Per-cycle short lines (last 5 only — full detail is in events.db).
    if summary.cycles:
        click.echo("Cycles (last 5):")
        for cs in summary.cycles[-5:]:
            qh = " [quiet-hours]" if cs.apply_skipped_quiet_hours else ""
            paused = " [paused]" if cs.paused else ""
            errs = f" errors={list(cs.stage_errors.keys())}" if cs.stage_errors else ""
            click.echo(f"  - cycle={cs.cycle} elapsed={cs.elapsed_s:.1f}s{qh}{paused}{errs}")

    # CI / cron friendliness: any cycle-level error is a non-zero exit (a stage
    # crash is bad even when we kept the loop alive).
    sys.exit(1 if summary.total_errors else 0)


@cli.command("prune")
@click.option("--ephemeral-days", type=int, default=None,
              help="Override settings.retention.ephemeral_days for this run.")
@click.option("--events-days", type=int, default=None,
              help="Override settings.retention.events_days for this run.")
def prune_cmd(ephemeral_days: int | None, events_days: int | None) -> None:
    """Delete ephemeral data older than the retention windows (spec section 4).

    Two scopes:
      * jobs in EPHEMERAL_STATES (SKIPPED/FILTERED) older than
        --ephemeral-days. APPLIED is kept indefinitely (dedup source of truth).
      * events.db rows older than --events-days (higher write volume, shorter window).

    Both run inside their own transactions; a crash mid-delete rolls back.
    """
    from auto_applier.pipeline.retention import prune_ephemeral, prune_events

    settings = load_settings()
    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    eff_eph = ephemeral_days if ephemeral_days is not None else settings.retention.ephemeral_days
    eff_evt = events_days if events_days is not None else settings.retention.events_days

    try:
        job_result = prune_ephemeral(conn, eff_eph)
    finally:
        conn.close()
    evt_result = prune_events(settings.events_db_path, eff_evt)

    click.echo(
        f"pruned jobs={job_result.deleted} cutoff={job_result.cutoff_iso} "
        f"(ephemeral_days={eff_eph})"
    )
    click.echo(
        f"pruned events={evt_result.deleted} cutoff={evt_result.cutoff_iso} "
        f"(events_days={eff_evt})"
    )


@cli.command("serve")
@click.option("--host", type=str, default=None,
              help="Bind address. Overrides settings.web.host (default 127.0.0.1). "
                   "Set to 0.0.0.0 to expose to the LAN — the local-first default "
                   "stays on localhost so a runner box doesn't silently leak state.")
@click.option("--port", type=int, default=None,
              help="Listen port. Overrides settings.web.port (default 8765).")
@click.option("--no-scheduler", is_flag=True, default=False,
              help="Start the web UI WITHOUT the background scheduler (read-only "
                   "diagnostics mode). Read-only API endpoints still answer; no "
                   "pipeline work happens. Useful when the fact bank / resume "
                   "aren't ready yet (pre-onboarding) so the dashboard can still "
                   "load to walk the user through setup.")
@click.option("--quiet-hours", type=str, default=None,
              help="Local-time HH:MM-HH:MM window during which the apply worker "
                   "pauses. Overrides settings.scheduler.quiet_hours.")
@click.option("--cycle-interval-s", type=float, default=None,
              help="Seconds between scheduler cycles. Overrides "
                   "settings.scheduler.cycle_interval_s.")
@click.option("--dry-run/--no-dry-run", default=True,
              help="Dev-safe default. --no-dry-run lets the apply worker SEND REAL "
                   "APPLICATIONS in mode=auto.")
@click.option("--mode", type=click.Choice(["auto", "assisted"]), default="auto",
              help="Apply mode: auto = bot submits on clean forms; assisted = pre-fill, human submits.")
@click.option("--no-llm", is_flag=True, default=False,
              help="Skip Ollama wiring. Filter fail-opens, score+optimize "
                   "fail-CLOSED, apply resolver uses bank + sensitive policy only.")
@click.option("--no-hotkey", is_flag=True, default=False,
              help="Disable the F6 control-handoff hotkey. Default is enabled "
                   "on Windows (soft-fails on other platforms). Override the "
                   "key in user_config.json: web.hotkey.")
@click.option("--idle-detect/--no-idle-detect", default=None,
              help="Override settings.web.idle_detect_enabled. When enabled, "
                   "the scheduler auto-pauses while the user is actively "
                   "interacting with the machine and resumes after "
                   "settings.web.idle_threshold_s of no input.")
def serve_cmd(host: str | None, port: int | None, no_scheduler: bool,
              quiet_hours: str | None, cycle_interval_s: float | None,
              dry_run: bool, mode: str, no_llm: bool,
              no_hotkey: bool, idle_detect: bool | None) -> None:
    """Run the local web UI + background worker service (spec section 11b Phase 4).

    The Phase 4 (1/M) entry: starts the FastAPI app on http://host:port and
    (unless --no-scheduler) boots the staged-worker scheduler as a background
    asyncio task. The dashboard is a Phase 4 (2/M) deliverable; right now the
    splash page lists the read-only JSON API endpoints.

    Lifecycle: SIGINT / Ctrl-C triggers a clean shutdown — the scheduler task
    is cancelled and awaited before uvicorn exits.
    """
    import asyncio
    import uvicorn

    from auto_applier.domain.state import ApplyMode
    from auto_applier.llm.complete import build_default
    from auto_applier.llm.embed import OllamaEmbeddings
    from auto_applier.pipeline import (
        ApplyWorker,
        DiscoverWorker,
        FilterWorker,
        OptimizeWorker,
        Scheduler,
        ScoreWorker,
        default_drivers,
        parse_quiet_hours,
    )
    from auto_applier.resume.factbank import FactBank
    from auto_applier.sources.browser.session import BrowserSession
    from auto_applier.telemetry import configure_sink
    from auto_applier.web import SchedulerService, WebState, create_app

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

    effective_host = host if host is not None else settings.web.host
    effective_port = port if port is not None else settings.web.port

    conn = init_app_db(settings.app_db_path)
    _install_sink(settings)

    web_state = WebState(
        settings=settings,
        app_db_path=settings.app_db_path,
        events_db_path=settings.events_db_path,
    )

    # Decide whether to spin up the scheduler. Pre-onboarding the fact bank
    # and resume don't exist yet; rather than failing the whole command, drop
    # into read-only diagnostics mode so the user can still reach the (future
    # 5/M) onboarding wizard from the dashboard.
    fact_bank_path = settings.data_dir / "profile" / "master.json"
    resume_path = settings.artifacts_dir / "resume.pdf"
    scheduler_ready = fact_bank_path.exists() and resume_path.exists()

    service: SchedulerService | None = None
    # Holder for the BrowserSession the scheduler factory starts. Defined at
    # function scope so the (4/M) headed launcher's ``new_page`` closure can
    # reach it from outside the scheduler-ready branch. Stays ``None`` when
    # no scheduler is constructed (``--no-scheduler`` or missing
    # prerequisites) — the launcher then falls back to the OS default browser.
    _session_holder: dict[str, BrowserSession | None] = {"session": None}

    if no_scheduler:
        click.echo("! --no-scheduler: read-only diagnostics mode "
                   "(read-only API answers; no pipeline work).")
    elif not scheduler_ready:
        # ASCII-only echo to match doctor/status output style.
        click.echo(
            "! scheduler not started: missing prerequisites — run onboarding "
            "first.",
            err=True,
        )
        if not fact_bank_path.exists():
            click.echo(f"        fact bank: missing at {fact_bank_path}", err=True)
        if not resume_path.exists():
            click.echo(f"        resume:    missing at {resume_path}", err=True)
        click.echo(
            "        web UI still available at "
            f"http://{effective_host}:{effective_port} (read-only).",
            err=True,
        )
    else:
        # Build the factory the SchedulerService will call with our pause
        # predicate. Same construction as ``av3 run`` (see run_cmd) — keep in
        # sync if knobs change.
        bank = FactBank.load(fact_bank_path)

        embed = None if no_llm else OllamaEmbeddings(
            host=settings.llm.ollama_host, model=settings.llm.embed_model
        )
        llm = None if no_llm else build_default(settings)
        apply_mode = ApplyMode.BROWSER_AUTO if mode == "auto" else ApplyMode.BROWSER_ASSISTED

        effective_cycle_interval = (
            cycle_interval_s if cycle_interval_s is not None
            else settings.scheduler.cycle_interval_s
        )
        effective_quiet_hours_raw = (
            quiet_hours if quiet_hours is not None
            else settings.scheduler.quiet_hours
        )
        quiet_hours_window = parse_quiet_hours(effective_quiet_hours_raw)

        if not dry_run:
            click.echo(
                f"! --no-dry-run: scheduler will SEND REAL APPLICATIONS in mode={mode} "
                f"(quiet_hours={effective_quiet_hours_raw or 'none'})"
            )

        from auto_applier.pipeline.retention import (
            prune_ephemeral as _prune_ephemeral,
            prune_events as _prune_events,
            run_backup_cycle as _run_backup_cycle,
        )

        async def _maintenance():
            _prune_ephemeral(conn, settings.retention.ephemeral_days)
            _prune_events(settings.events_db_path, settings.retention.events_days)
            _run_backup_cycle(settings)

        # The BrowserSession is started lazily inside the factory so the
        # uvicorn event loop owns its lifecycle. The factory runs inside
        # SchedulerService.start() (already async); the teardown closure
        # mirror-runs inside SchedulerService.stop(). ``_session_holder`` is
        # defined at function scope above so the (4/M) headed launcher can
        # reach the session from outside this branch.

        async def _factory(pause_predicate):
            session = BrowserSession(settings.browser_profile_dir)
            await session.start()
            _session_holder["session"] = session
            return Scheduler(
                discover_worker=DiscoverWorker(settings=settings, conn=conn),
                filter_worker=FilterWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    embed_client=embed,
                ),
                score_worker=ScoreWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    llm_client=llm,
                ),
                optimize_worker=OptimizeWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    llm_client=llm,
                ),
                apply_worker=ApplyWorker(
                    settings=settings, conn=conn, fact_bank=bank,
                    resume_path=str(resume_path), new_page=session.new_page,
                    embed_client=embed, llm_client=llm, mode=apply_mode,
                    dry_run=dry_run, drivers=default_drivers(),
                ),
                cycle_interval_s=effective_cycle_interval,
                quiet_hours=quiet_hours_window,
                pause_predicate=pause_predicate,
                maintenance=_maintenance,
                maintenance_interval_s=settings.retention.maintenance_interval_s,
            )

        async def _teardown():
            sess = _session_holder.get("session")
            if sess is not None:
                await sess.stop()
                _session_holder["session"] = None

        service = SchedulerService(_factory, teardown=_teardown)

    # (3/M) — F6 hotkey + idle-detect watchers. Both are optional; both
    # share the service's ControlState so manual + hotkey + idle pause
    # sources OR cleanly in the predicate. Only attach when a service
    # exists — a read-only diagnostics mode has nothing to pause.
    watchers: list = []
    if service is not None:
        effective_hotkey_enabled = (
            settings.web.hotkey_enabled and not no_hotkey
        )
        effective_idle_enabled = (
            idle_detect if idle_detect is not None
            else settings.web.idle_detect_enabled
        )
        if effective_hotkey_enabled:
            from auto_applier.web.hotkey import HotkeyWatcher, build_hotkey_toggle
            watchers.append(HotkeyWatcher(
                on_toggle=build_hotkey_toggle(service),
                key=settings.web.hotkey,
            ))
        if effective_idle_enabled:
            from auto_applier.web.idle import IdleWatcher
            watchers.append(IdleWatcher(
                control=service.control,
                idle_threshold_s=settings.web.idle_threshold_s,
                poll_interval_s=settings.web.idle_poll_s,
            ))

    # (4/M) — headed launcher used by login-on-demand + assisted submit
    # endpoints. Bind it to the BrowserSession's ``new_page`` so URLs open
    # in the bot's persistent Chrome profile (cookies land in the right
    # jar for the next apply cycle). The session lives inside the factory's
    # holder so we close over the holder, not the not-yet-built session.
    from auto_applier.web.headed import HeadedBrowserLauncher

    async def _launcher_new_page():
        sess = _session_holder.get("session") if service is not None else None
        if sess is None:
            raise RuntimeError("BrowserSession not started")
        return await sess.new_page()

    launcher = HeadedBrowserLauncher(
        new_page=_launcher_new_page if service is not None else None,
    )

    app = create_app(
        state=web_state,
        service=service,
        watchers=watchers or None,
        launcher=launcher,
    )

    hotkey_label = (
        settings.web.hotkey if service is not None and any(
            type(w).__name__ == "HotkeyWatcher" for w in watchers
        ) else "off"
    )
    idle_label = (
        "on" if service is not None and any(
            type(w).__name__ == "IdleWatcher" for w in watchers
        ) else "off"
    )
    click.echo(
        f"Starting av3 web UI on http://{effective_host}:{effective_port} "
        f"(scheduler={'on' if service is not None else 'off'} "
        f"hotkey={hotkey_label} idle-detect={idle_label})"
    )

    try:
        uvicorn.run(
            app,
            host=effective_host,
            port=effective_port,
            log_level="info",
            access_log=False,  # quiet — events.db is the audit trail
        )
    finally:
        conn.close()


@cli.command("launch")
@click.option("--host", type=str, default=None,
              help="Bind address (default settings.web.host, typically 127.0.0.1).")
@click.option("--port", type=int, default=None,
              help="Listen port (default settings.web.port, typically 8765).")
@click.option("--browser/--no-browser", default=True,
              help="Open the default browser to the dashboard once the server "
                   "is reachable. --no-browser is useful when launching on a "
                   "headless runner box.")
@click.option("--probe-timeout-s", type=float, default=15.0,
              help="How long to wait for the server to start accepting "
                   "connections before opening the browser. The probe gives "
                   "up after this and opens the URL anyway -- the user sees "
                   "an error in the tab if the server failed to boot.")
def launch_cmd(host: str | None, port: int | None,
               browser: bool, probe_timeout_s: float) -> None:
    """One-click launcher: starts av3 serve in a child process, waits for the
    HTTP port to accept connections, then opens the default browser to the
    dashboard (spec section 11a "one-click launcher starts the worker+server
    and opens the dashboard tab").

    This is the spec's non-technical-user entry point — what the bundled
    installer's shortcut runs. Power users keep running ``av3 serve``
    directly; this command exists so the runner box's autostart and the
    Windows ``av3-launcher.cmd`` shortcut both reduce to a single
    ``av3 launch`` call.

    The server process inherits this terminal's stdio so its logs stream
    here; Ctrl-C terminates both processes cleanly via the child's signal
    handler. On Windows the .cmd wrapper hides this window for the
    non-technical UX; on macOS/Linux the user sees the same logs as they
    would from ``av3 serve``.
    """
    import socket
    import subprocess
    import sys
    import time
    import webbrowser

    settings = load_settings()
    effective_host = host if host is not None else settings.web.host
    effective_port = port if port is not None else settings.web.port

    # The dashboard URL: localhost is the only safe target even when bound
    # to 0.0.0.0 — opening the LAN-facing host would force the user's
    # browser through their gateway, which fails on the local network for
    # most home setups.
    probe_host = "127.0.0.1" if effective_host in ("0.0.0.0", "::") else effective_host
    dashboard_url = f"http://{probe_host}:{effective_port}/"

    # Spawn ``av3 serve`` as a child process so this launcher can do the
    # port-probe + browser-open work while the server is still starting.
    # Pass through the host/port flags so the child agrees with the URL we
    # eventually open. We invoke through ``sys.executable -m auto_applier.cli.main``
    # rather than the installed ``av3`` script so the launcher works even
    # in a fresh checkout where ``pip install -e .`` hasn't run yet — the
    # console script may be missing, but the module path is always
    # importable from the repo root.
    # In a PyInstaller-frozen exe, ``sys.executable`` IS the bundled app and the
    # ``-m auto_applier.cli.main`` form does NOT work (the bootloader runs run_v3.py, which
    # forwards argv straight to the Click group). So the frozen launcher spawns
    # ``<exe> serve ...`` and the source launcher spawns ``python -m auto_applier.cli.main
    # serve ...``. (Phase 5 5/M — bundled-installer correctness.)
    if getattr(sys, "frozen", False):
        child_args = [sys.executable, "serve",
                      "--host", str(effective_host), "--port", str(effective_port)]
    else:
        child_args = [
            sys.executable, "-m", "auto_applier.cli.main", "serve",
            "--host", str(effective_host),
            "--port", str(effective_port),
        ]
    click.echo(f"Launching av3 server at {dashboard_url} ...")

    # On Windows we don't want a separate console window for the child —
    # the launcher's window already holds stdio. On POSIX inherit normally.
    creationflags = 0
    if sys.platform == "win32":
        # 0x00000008 = DETACHED_PROCESS would orphan stdio; we'd rather
        # the child stay attached so logs stream and Ctrl-C propagates.
        # Just inherit the parent's console.
        creationflags = 0

    try:
        child = subprocess.Popen(child_args, creationflags=creationflags)
    except FileNotFoundError:
        click.echo(
            "! could not find a Python interpreter to launch av3 serve",
            err=True,
        )
        sys.exit(1)

    if browser:
        _wait_for_port(probe_host, effective_port, timeout_s=probe_timeout_s)
        try:
            webbrowser.open(dashboard_url)
        except Exception as exc:  # noqa: BLE001 — never fail launch on browser issues
            click.echo(f"! could not open browser: {exc} (visit {dashboard_url} manually)",
                       err=True)
    else:
        click.echo(f"--no-browser: leaving the dashboard at {dashboard_url}")

    # Wait on the child so Ctrl-C in this terminal cleanly stops the server.
    try:
        rc = child.wait()
    except KeyboardInterrupt:
        # Forward the signal — the child's uvicorn handler will tear down
        # the lifespan (scheduler + watchers) cleanly.
        try:
            child.terminate()
        except Exception:
            pass
        rc = child.wait()
    sys.exit(rc)


def _wait_for_port(host: str, port: int, *, timeout_s: float) -> bool:
    """Poll-connect until the server accepts connections or we hit timeout.

    Returns True iff the port opened. We never raise — a failed probe
    just means we open the browser early and the user sees an error in
    the tab (better than the launcher hanging silently on a stuck child).
    """
    import socket
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.25)
    return False


# --------------------------------------------------------------- observability
#
# ``av3 errors`` / ``av3 stats`` (Phase 5 1/M, spec section 11b).
#
# Both read events.db directly through the EventSink helper queries. They are
# the "Claude debug session straight from SQL — no log files" surface from
# spec section 9. Local-only — they DO NOT depend on Phase 5 (2/M)'s opt-in
# remote mirror, so they work the moment events exist.

# Accept short relative windows (e.g. ``5m``, ``2h``, ``7d``). The CLI owns
# parsing so the sink stays pure-DB; the cutoff reaches the sink as an
# ISO-8601 timestamp.
_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_since(value: str | None) -> str | None:
    """Return an ISO-8601 UTC cutoff for ``--since 30m|24h|7d`` style input.

    Returns ``None`` when no window was requested. Raises ``click.BadParameter``
    on unparseable input so the user sees the failure at the CLI, not as a
    silently-empty result.
    """
    if value is None:
        return None
    raw = value.strip().lower()
    # Reject anything with embedded whitespace or non-ASCII digits — ``int()``
    # accepts strings like ``"30 "`` and Unicode digit chars, which would
    # otherwise sneak through a sloppy parse.
    if not raw or len(raw) < 2 or any(c.isspace() for c in raw):
        raise click.BadParameter(f"unrecognized --since value {value!r} (try 30m, 2h, 7d)")
    unit = raw[-1]
    if unit not in _SINCE_UNITS:
        raise click.BadParameter(f"unrecognized --since value {value!r} (try 30m, 2h, 7d)")
    digits = raw[:-1]
    if not digits.isascii() or not digits.lstrip("-").isdigit():
        raise click.BadParameter(f"unrecognized --since value {value!r} (try 30m, 2h, 7d)")
    try:
        n = int(digits)
    except ValueError as exc:
        raise click.BadParameter(f"unrecognized --since value {value!r} (try 30m, 2h, 7d)") from exc
    if n <= 0:
        raise click.BadParameter(f"--since must be positive (got {value!r})")

    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=n * _SINCE_UNITS[unit])
    # Match the ``utcnow_iso`` shape that ``EventSink.emit`` writes — strict
    # lexicographic compare in SQL only works when both sides share the format.
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


def _truncate(text: str | None, width: int) -> str:
    """ASCII-safe column truncator. Empty string for ``None``."""
    if not text:
        return ""
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "~"


@cli.command("errors")
@click.option("--limit", type=int, default=25, show_default=True,
              help="Maximum rows to return. Default sized for one terminal screen.")
@click.option("--stage", type=str, default=None,
              help="Filter by stage label (e.g. 'apply', 'score', 'optimize').")
@click.option("--platform", type=str, default=None,
              help="Filter by source/platform (e.g. 'greenhouse', 'lever', 'ashby').")
@click.option("--since", "since", type=str, default=None,
              help="Only show errors newer than this relative window. "
                   "Form: <int><s|m|h|d> (e.g. 30m, 24h, 7d).")
@click.option("--run-id", "run_id", type=str, default=None,
              help="Filter by run_id — focused triage of one apply cycle.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a JSON array of error rows instead of the ASCII table. "
                   "Pipe through `jq -c '.[]'` for NDJSON.")
def errors_cmd(limit: int, stage: str | None, platform: str | None,
               since: str | None, run_id: str | None, as_json: bool) -> None:
    """Show recent error events from events.db (spec section 9).

    The local-only triage surface: every @stage wrapper writes a row on
    failure; this command surfaces them with optional filters. Always exits 0
    (asking for errors is not itself an error condition — that would break
    `av3 errors --json | jq` pipelines).
    """
    import json as _json

    settings = load_settings()
    since_iso = _parse_since(since)
    sink = EventSink(settings.events_db_path)
    try:
        rows = sink.query_errors(
            since_iso=since_iso, stage=stage, platform=platform,
            run_id=run_id, limit=limit,
        )
    finally:
        sink.close()

    if as_json:
        click.echo(_json.dumps([dict(r) for r in rows], indent=2, default=str))
        return

    if not rows:
        click.echo("No matching error events.")
        return

    # Fixed-width ASCII table (cp1252 console safe). Widths picked to fit a
    # typical 120-col terminal; longer values get truncated with a trailing '~'.
    header = (
        f"{'ts':19}  {'stage':12}  {'platform':10}  "
        f"{'job_id':14}  {'error_type':22}  {'run_id':10}  msg"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        # ts column: strip fractional seconds for compactness.
        ts = (r["ts"] or "")[:19]
        click.echo(
            f"{_truncate(ts, 19):19}  "
            f"{_truncate(r['stage'], 12):12}  "
            f"{_truncate(r['platform'], 10):10}  "
            f"{_truncate(r['job_id'], 14):14}  "
            f"{_truncate(r['error_type'], 22):22}  "
            f"{_truncate(r['run_id'], 10):10}  "
            f"{_truncate(r['error_msg'], 60)}"
        )
    click.echo(f"\n{len(rows)} row(s).")


@cli.command("stats")
@click.option("--since", "since", type=str, default=None,
              help="Only count events newer than this relative window. "
                   "Form: <int><s|m|h|d> (e.g. 30m, 24h, 7d).")
@click.option("--platform", type=str, default=None,
              help="Filter by source/platform.")
@click.option("--run-id", "run_id", type=str, default=None,
              help="Filter by run_id — per-cycle health view.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a JSON array of {stage, ok, error, skip, avg_ms} "
                   "rows instead of the ASCII table.")
def stats_cmd(since: str | None, platform: str | None,
              run_id: str | None, as_json: bool) -> None:
    """Per-stage event aggregates from events.db (spec section 9).

    Quick health check after a run: ok / error / skip counts plus mean
    duration per stage. The high-level "is the pipeline broken?" view that
    sits opposite `av3 errors`' per-row triage. Always exits 0.
    """
    import json as _json

    settings = load_settings()
    since_iso = _parse_since(since)
    sink = EventSink(settings.events_db_path)
    try:
        rows = sink.query_stats(
            since_iso=since_iso, platform=platform, run_id=run_id,
        )
    finally:
        sink.close()

    if as_json:
        # Normalize Row → dict and round avg_ms so consumers don't get a
        # floating-point tail that won't round-trip through diff tools.
        out = []
        for r in rows:
            d = dict(r)
            if d.get("avg_ms") is not None:
                d["avg_ms"] = round(d["avg_ms"], 1)
            out.append(d)
        click.echo(_json.dumps(out, indent=2, default=str))
        return

    if not rows:
        click.echo("No events in window.")
        return

    header = f"{'stage':16}  {'ok':>6}  {'error':>6}  {'skip':>6}  {'avg_ms':>8}"
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        avg = r["avg_ms"]
        avg_s = f"{avg:.0f}" if avg is not None else "-"
        click.echo(
            f"{_truncate(r['stage'], 16):16}  "
            f"{(r['ok'] or 0):>6}  "
            f"{(r['error'] or 0):>6}  "
            f"{(r['skip'] or 0):>6}  "
            f"{avg_s:>8}"
        )


# --------------------------------------------------------------- telemetry
#
# ``av3 telemetry on|off|status`` (Phase 5 3/M, spec §9).
#
# The opt-in manager for the remote mirror. ``on`` is the ONLY place a CLI user
# flips the single network-egress switch in the product, so it explains exactly
# what leaves the machine before enabling. The mirror plumbing (queue + scrubbers)
# shipped in 2/M; the relay client that drains the queue lands in 4/M — so until
# then ``on`` just starts *enqueueing* scrubbed rows locally.


def _prompt_for_handle() -> str:
    """Interactive handle prompt shared by ``telemetry on`` (and reused by any
    future first-run path). We send ``user_id = sha256(handle)[:10]`` (§9); the
    raw handle stays local in user_config. Loops until non-empty so we never
    store a blank handle that would hash to a useless ``anonymous`` pseudonym.
    """
    while True:
        handle = click.prompt(
            "Enter a handle/first name (stored locally; we send only its hash)",
            type=str,
            default="",
            show_default=False,
        ).strip()
        if handle:
            return handle
        click.echo("  (a handle is required to attribute your reports — try again)")


@cli.group("telemetry")
def telemetry_group() -> None:
    """Manage the opt-in remote error mirror (spec §9). Default OFF."""


@telemetry_group.command("on")
@click.option("--handle", "handle", type=str, default=None,
              help="Set the attribution handle non-interactively (sha256[:10] is "
                   "sent; the raw handle stays local). Prompts if omitted and none "
                   "is stored yet.")
@click.option("--relay-url", "relay_url", type=str, default=None,
              help="Set the owner-hosted relay endpoint the drainer POSTs to "
                   "(Phase 5 4/M). Optional now; required before `av3 mirror drain` "
                   "can send.")
def telemetry_on(handle: str | None, relay_url: str | None) -> None:
    """Opt IN to the remote error mirror (spec §9).

    Explains exactly what leaves the machine, captures/keeps a local handle, and
    flips ``telemetry.enabled = True`` in user_config. From the next worker run,
    error + inferred-answer events are scrubbed and enqueued for the relay client.
    """
    from auto_applier.telemetry import user_id_from_handle
    from auto_applier.web.onboarding import load_user_config, save_user_config

    settings = load_settings()
    cfg = load_user_config(settings.data_dir)
    telemetry = dict(cfg.get("telemetry") or {})

    effective_handle = handle or telemetry.get("handle")
    if not effective_handle:
        # The §9 disclosure, shown BEFORE we ask for anything.
        click.echo(
            "Telemetry opt-in (spec §9) — what leaves your machine when enabled:\n"
            "  * errors/critical: stage, platform, error_type, a SCRUBBED error "
            "message (no paths/emails/phones), app version, timestamp.\n"
            "  * inferred-answer events: the question text (scrubbed), its category, "
            "the model's confidence, and whether it answered or bailed —\n"
            "    NEVER the answer value itself, and EEO questions are never sent.\n"
            "  * an attribution id = sha256(handle)[:10]; your raw handle stays local.\n"
            "Everything is stored locally in events.db regardless; this only controls "
            "the scrubbed REMOTE copy.\n"
        )
        effective_handle = _prompt_for_handle()

    telemetry["handle"] = effective_handle
    telemetry["enabled"] = True
    if relay_url is not None:
        telemetry["relay_url"] = relay_url
    cfg["telemetry"] = telemetry
    save_user_config(settings.data_dir, cfg)

    user_id = user_id_from_handle(effective_handle)
    click.echo(f"+ telemetry ENABLED. You'll send as user_id={user_id}")
    if not telemetry.get("relay_url"):
        click.echo(
            "  note: no relay_url set yet — scrubbed rows queue locally until you "
            "set one (`av3 telemetry on --relay-url ...`) and the relay client "
            "(Phase 5 4/M) drains them."
        )


@telemetry_group.command("off")
def telemetry_off() -> None:
    """Opt OUT. Flips ``telemetry.enabled = False``; the local events.db is
    untouched. Already-queued scrubbed rows STAY (they're harmless and already
    scrubbed) — drain or prune them via the mirror tooling if you want them gone.
    """
    from auto_applier.web.onboarding import load_user_config, save_user_config

    settings = load_settings()
    cfg = load_user_config(settings.data_dir)
    telemetry = dict(cfg.get("telemetry") or {})
    telemetry["enabled"] = False
    cfg["telemetry"] = telemetry
    save_user_config(settings.data_dir, cfg)

    # Surface any rows that were enqueued while enabled so "off" isn't silently
    # leaving a backlog the user forgot about.
    sink = EventSink(settings.events_db_path)
    try:
        pending = sink.mirror_queue.pending_count()
    finally:
        sink.close()
    click.echo("+ telemetry DISABLED. Local events.db is unchanged.")
    if pending:
        click.echo(
            f"  note: {pending} scrubbed row(s) remain queued. They won't be sent "
            "while disabled; `av3 mirror drain` (after re-enabling) or a prune clears them."
        )


@telemetry_group.command("status")
def telemetry_status() -> None:
    """Show telemetry state: enabled?, your user_id, relay_url, and the mirror
    queue depth / last enqueue / last failure (spec §9)."""
    from auto_applier.telemetry import MirrorPolicy

    settings = load_settings()
    policy = MirrorPolicy.from_settings(settings.telemetry, __version__)

    click.echo(f"enabled:    {settings.telemetry.enabled}")
    click.echo(f"user_id:    {policy.user_id}"
               + ("  (no handle set — would send as 'anonymous')"
                  if policy.user_id == "anonymous" else ""))
    click.echo(f"relay_url:  {settings.telemetry.relay_url or '(unset)'}")

    sink = EventSink(settings.events_db_path)
    try:
        s = sink.mirror_queue.summary()
    finally:
        sink.close()
    click.echo("mirror queue:")
    click.echo(f"  pending:        {s['pending']}")
    click.echo(f"  delivered:      {s['delivered']}")
    click.echo(f"  last enqueued:  {s['last_enqueued_at'] or '-'}")
    if s["last_error"]:
        click.echo(
            f"  last failure:   {s['last_error']} "
            f"(attempt {s['last_error_attempts']}, next retry {s['next_retry_at']})"
        )


@cli.command("export-diagnostics")
@click.option("--raw", is_flag=True, default=False,
              help="Include a verbatim events.db copy + un-scrubbed error messages. "
                   "PII-BEARING — only for in-group debugging where the owner is the "
                   "recipient. Default is the scrubbed bundle, safe to email anywhere.")
@click.option("--error-limit", type=int, default=200, show_default=True,
              help="Max recent error/inferred rows to include in the JSON exports.")
def export_diagnostics_cmd(raw: bool, error_limit: int) -> None:
    """Bundle local diagnostics into a single tarball for support (spec §9).

    "Send me a tarball" instead of "send me your logs." Contents: settings
    (secrets always stripped), doctor results, recent error + inferred-answer
    events, per-stage stats, mirror-queue status, and a manifest. Default is
    SCRUBBED (safe to share); --raw adds the full events.db (PII-bearing).
    """
    from auto_applier.telemetry.diagnostics import build_diagnostics

    settings = load_settings()

    if raw:
        click.echo(
            "! --raw: this bundle includes your full events.db and un-scrubbed "
            "error messages (paths/emails/phones may appear). Share only with the "
            "owner.",
        )

    result = build_diagnostics(settings, raw=raw, error_limit=error_limit)
    kb = result.bytes_written / 1024
    click.echo(
        f"Wrote {result.path} ({kb:.1f} KB, {result.error_rows} error row(s), "
        f"mode={'raw' if result.raw else 'scrubbed'})"
    )


@cli.group("mirror")
def mirror_group() -> None:
    """Drain / manage the opt-in telemetry mirror queue (spec §9, Phase 5 4/M)."""


@mirror_group.command("drain")
@click.option("--limit", type=int, default=50, show_default=True,
              help="Max queued rows to POST this pass. The queue is durable; "
                   "undelivered rows retry on the next drain.")
@click.option("--timeout-s", type=float, default=10.0, show_default=True,
              help="Per-request HTTP timeout to the relay.")
def mirror_drain_cmd(limit: int, timeout_s: float) -> None:
    """POST queued, scrubbed telemetry rows to the owner-hosted relay (spec §9).

    The out-of-band drainer: schedule it on your own cron / Task Scheduler so a
    slow relay never blocks the pipeline. Gated — does nothing unless telemetry
    is enabled AND a relay_url is set. Always exits 0 on transient relay failure
    (rows retry via the backoff ladder); exits 2 only on a config problem.
    """
    from auto_applier.telemetry.client import MirrorClient

    settings = load_settings()

    if not settings.telemetry.enabled:
        click.echo("telemetry disabled — nothing to send (`av3 telemetry on` to enable).")
        return
    if not settings.telemetry.relay_url:
        click.echo("  x FAIL no relay_url set", err=True)
        click.echo(
            "        fix -> `av3 telemetry on --relay-url https://<your-relay>`",
            err=True,
        )
        sys.exit(2)

    sink = EventSink(settings.events_db_path)
    try:
        client = MirrorClient(
            sink.mirror_queue, settings.telemetry.relay_url, timeout_s=timeout_s
        )
        result = client.drain(limit=limit)
        pending = sink.mirror_queue.pending_count()
    finally:
        sink.close()

    click.echo(
        f"drained attempted={result.attempted} delivered={result.delivered} "
        f"failed={result.failed} still_pending={pending}"
    )
    # Always exit 0: a failed POST is a transient REMOTE condition (relay redeploy,
    # network blip), not a local error, and the backoff ladder retries it. Failing
    # here would flap a user's cron alert on every brief relay outage. A genuine
    # config problem (no relay_url) already exited 2 above.


@cli.command("update")
@click.option("--repo", type=str, default=None,
              help="GitHub slug to check (default the project repo).")
@click.option("--timeout-s", type=float, default=5.0, show_default=True,
              help="HTTP timeout for the release-feed fetch.")
@click.option("--exit-code", is_flag=True, default=False,
              help="Exit 10 when an update is available (for scripting / a "
                   "launch-time check). Default always exits 0.")
def update_cmd(repo: str | None, timeout_s: float, exit_code: bool) -> None:
    """Check the release feed and prompt if a newer version exists (spec §11a).

    v3.0 is check + prompt only — it tells you where to get the new installer;
    it does NOT auto-replace a running service (that's its own risk surface).
    Never fails the command on a network problem — a missed check is not an error.
    """
    from auto_applier.update import DEFAULT_REPO, check_for_update

    effective_repo = repo or DEFAULT_REPO
    info = check_for_update(__version__, repo=effective_repo, timeout_s=timeout_s)

    if info is None:
        click.echo(
            f"Could not reach the release feed for {effective_repo} "
            f"(offline / rate-limited?). Current version {__version__}."
        )
        return  # exit 0 — a missed check is not an error
    if info.is_newer:
        click.echo(
            f"Update available: {info.current} -> {info.latest}\n"
            f"  {info.url}\n"
            "  Download and run the new installer to update."
        )
        if exit_code:
            sys.exit(10)
    else:
        click.echo(f"Up to date (current {info.current}, latest {info.latest}).")


@cli.command("install-browser")
@click.option("--backend", type=click.Choice(["auto", "patchright", "playwright"]),
              default="auto", show_default=True,
              help="Which package's `install chromium` to run. 'auto' prefers "
                   "patchright (the stealth backend) and falls back to playwright.")
def install_browser_cmd(backend: str) -> None:
    """Download the Chromium browser binary (first-run / installer step, spec §11a).

    The bundled installer ships the Python app lean; the browser is fetched here
    on first launch. Real Chrome (via channel) is the primary apply path (spec
    §8c) — this Chromium is the stealth driver + the busy-Chrome fallback the
    BrowserSession uses. Idempotent: re-running just re-verifies the download.
    """
    import subprocess

    order = (
        ["patchright", "playwright"] if backend == "auto" else [backend]
    )
    last_err = ""
    for pkg in order:
        click.echo(f"Installing Chromium via {pkg} ...")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", pkg, "install", "chromium"],
                capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            last_err = f"{pkg}: {exc}"
            continue
        if proc.returncode == 0:
            click.echo(f"+ Chromium installed via {pkg}.")
            return
        last_err = (proc.stderr or proc.stdout or "").strip()[:300]
        click.echo(f"! {pkg} install failed (rc={proc.returncode}); trying next backend.",
                   err=True)
    click.echo(f"  x FAIL could not install Chromium: {last_err}", err=True)
    click.echo(
        "        fix -> ensure the v3 extras are installed "
        "(`pip install -e \".[v3]\"`) then re-run `av3 install-browser`.",
        err=True,
    )
    sys.exit(1)


@cli.command("backup")
def backup_cmd() -> None:
    """Snapshot app.db + events.db; rotate older snapshots (spec section 4).

    Uses SQLite's online backup API so it's safe while the DB is in use. Both
    backups are attempted even if one fails; failure is reported in the
    summary line with a non-zero exit code so cron / monitoring catches it.
    """
    from auto_applier.pipeline.retention import run_backup_cycle

    settings = load_settings()
    _install_sink(settings)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)

    summary = run_backup_cycle(settings)

    for b in summary.backups:
        click.echo(
            f"{b.db_label}: snapshot={b.snapshot_path.name} rotated={b.rotated}"
        )
    for db_label, err in summary.errors.items():
        click.echo(f"  x FAIL {db_label}: {err}", err=True)

    sys.exit(0 if summary.ok else 1)


if __name__ == "__main__":
    cli()

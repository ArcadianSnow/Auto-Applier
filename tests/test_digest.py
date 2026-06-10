"""`av3 digest` read-side (ScoreRepo.list_ranked) — the discovery+scoring-only shortlist.

Contract:
  * scored jobs come back joined to their job row, ranked by score DESC;
  * ``min_total`` hides below-threshold noise;
  * ``limit`` caps the list to the top N;
  * no scores -> empty list (the command prints a "run the pipeline" hint).
"""

from __future__ import annotations

from auto_applier.db.repositories import JobRepo, ScoreRepo
from auto_applier.domain.models import Job, JobScore
from auto_applier.domain.state import JobState


def _seed_job(conn, *, jid: str, title: str, company: str) -> None:
    JobRepo(conn).add(
        Job(
            id=jid, source="greenhouse", source_job_id=jid,
            title=title, company=company, canonical_hash=f"h-{jid}",
            location="Remote", url=f"https://example/{jid}",
            state=JobState.DECIDED,
        )
    )


def test_list_ranked_orders_by_score_desc(conn):
    _seed_job(conn, jid="a", title="Data Engineer", company="Acme")
    _seed_job(conn, jid="b", title="Analytics Engineer", company="Globex")
    _seed_job(conn, jid="c", title="DBA", company="Initech")
    sr = ScoreRepo(conn)
    sr.upsert(JobScore(job_id="a", total=8.2, dimensions={"skills": 9}))
    sr.upsert(JobScore(job_id="b", total=6.5, dimensions={"skills": 7}))
    sr.upsert(JobScore(job_id="c", total=3.1, dimensions={"skills": 4}))

    ranked = sr.list_ranked()

    assert [r["job_id"] for r in ranked] == ["a", "b", "c"]
    top = ranked[0]
    assert top["company"] == "Acme"
    assert top["title"] == "Data Engineer"
    assert top["url"] == "https://example/a"
    assert top["total"] == 8.2


def test_list_ranked_min_total_filters(conn):
    _seed_job(conn, jid="a", title="x", company="A")
    _seed_job(conn, jid="b", title="y", company="B")
    sr = ScoreRepo(conn)
    sr.upsert(JobScore(job_id="a", total=8.0))
    sr.upsert(JobScore(job_id="b", total=3.0))

    ranked = sr.list_ranked(min_total=5.0)

    assert [r["job_id"] for r in ranked] == ["a"]


def test_list_ranked_limit_caps_to_top_n(conn):
    for i, total in enumerate([9.0, 7.0, 5.0]):
        _seed_job(conn, jid=f"j{i}", title="t", company=f"C{i}")
        ScoreRepo(conn).upsert(JobScore(job_id=f"j{i}", total=total))

    ranked = ScoreRepo(conn).list_ranked(limit=2)

    assert len(ranked) == 2
    assert [r["total"] for r in ranked] == [9.0, 7.0]


def test_list_ranked_empty_when_nothing_scored(conn):
    assert ScoreRepo(conn).list_ranked() == []

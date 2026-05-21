"""Tests for hermes_cli.cron command handling."""

from argparse import Namespace

import pytest

from cron.jobs import create_job, get_job, list_jobs
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:
    def test_pause_resume_run(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(Namespace(cron_command="run", job_id=job["id"]))
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out

    def test_edit_can_replace_and_clear_skills(self, tmp_cron_dir, capsys):
        job = create_job(
            prompt="Combine skill outputs",
            schedule="every 1h",
            skill="blogwatcher",
        )

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule="every 2h",
                prompt="Revised prompt",
                name="Edited Job",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["find-nearby", "blogwatcher"],
                clear_skills=False,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["find-nearby", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                clear_skills=True,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None

        out = capsys.readouterr().out
        assert "Updated job" in out

    def test_edit_skip_reflection_flag_sets_and_clears(self, tmp_cron_dir, capsys):
        """P58/MOL-268 — --skip-reflection {true,false} round-trips through the
        CLI → _cron_api → update_job chain and lands in jobs.json as a
        Python bool, preserving sibling fields. This is the sanctioned path
        for toggling reflection per-job without raw jobs.json edits.

        P69/MOL-277: Namespace hardened to include every cron_edit arg
        (script, add_skills, remove_skills) so the test survives future
        removal of getattr(args, ..., None) defaults in cron_edit handler."""
        # Use a helper to make the full arg set explicit + DRY. Defaults match
        # what the argparser would emit when the operator passes only --skip-reflection
        # (everything else None / False / []).
        def edit_ns(**overrides):
            base = dict(
                cron_command="edit",
                schedule=None, prompt=None, name=None,
                deliver=None, repeat=None,
                skill=None, skills=None,
                add_skills=None, remove_skills=None, clear_skills=False,
                script=None,
                skip_reflection=None,
            )
            base.update(overrides)
            return Namespace(**base)

        job = create_job(
            prompt="Chief, time to call the dentist.",
            schedule="0 10 * * 3",
            name="Call Dentist",
        )
        original_prompt = job["prompt"]
        original_name = job["name"]

        # Set skip_reflection=true
        cron_command(edit_ns(job_id=job["id"], skip_reflection="true"))
        updated = get_job(job["id"])
        assert updated["skip_reflection"] is True, (
            f"expected skip_reflection=True, got {updated.get('skip_reflection')!r}"
        )
        # Siblings untouched — the flag must not clobber prompt/name
        assert updated["prompt"] == original_prompt
        assert updated["name"] == original_name

        # Set skip_reflection=false
        cron_command(edit_ns(job_id=job["id"], skip_reflection="false"))
        toggled = get_job(job["id"])
        assert toggled["skip_reflection"] is False

        # Omitting the flag leaves skip_reflection unchanged
        cron_command(edit_ns(job_id=job["id"], prompt="Reworded"))
        unchanged = get_job(job["id"])
        assert unchanged["skip_reflection"] is False  # still False from prior call
        assert unchanged["prompt"] == "Reworded"

    def test_create_with_multiple_skills(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Use both skills",
                name="Skill combo",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["blogwatcher", "find-nearby"],
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "find-nearby"]
        assert jobs[0]["name"] == "Skill combo"

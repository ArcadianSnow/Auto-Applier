"""Tests for log_setup.py — rotating file handler setup."""

import logging
import time
from pathlib import Path

import pytest

from auto_applier import log_setup


@pytest.fixture(autouse=True)
def cleanup_handlers():
    """Ensure we clean up any file handlers after each test."""
    yield
    log_setup.stop_run_logging()


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_applier.log_setup.LOGS_DIR", tmp_path)
    return tmp_path


class TestStartRunLogging:
    def test_returns_path(self, logs_dir):
        path = log_setup.start_run_logging()
        assert isinstance(path, Path)
        assert path.parent == logs_dir
        assert path.suffix == ".log"
        assert "run-" in path.name

    def test_file_is_created(self, logs_dir):
        path = log_setup.start_run_logging()
        assert path.exists()

    def test_current_log_path(self, logs_dir):
        path = log_setup.start_run_logging()
        assert log_setup.current_log_path() == path

    def test_multiple_calls_create_new_handler(self, logs_dir):
        """Calling start twice replaces the old file handler."""
        p1 = log_setup.start_run_logging()
        # Sleep to ensure different timestamp
        time.sleep(1.1)
        p2 = log_setup.start_run_logging()
        assert p1 != p2
        assert p1.exists()
        assert p2.exists()

    def test_writes_to_file(self, logs_dir):
        path = log_setup.start_run_logging()
        logger = logging.getLogger("test_log_setup_writes")
        logger.debug("test message 12345")
        # Flush the handler
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.FileHandler):
                h.flush()
        content = path.read_text()
        assert "test message 12345" in content


class TestStopRunLogging:
    def test_clears_current_path(self, logs_dir):
        log_setup.start_run_logging()
        assert log_setup.current_log_path() is not None
        log_setup.stop_run_logging()
        assert log_setup.current_log_path() is None

    def test_stop_without_start_is_safe(self):
        log_setup.stop_run_logging()  # should not raise


class TestCurrentLogPath:
    def test_none_before_start(self):
        assert log_setup.current_log_path() is None

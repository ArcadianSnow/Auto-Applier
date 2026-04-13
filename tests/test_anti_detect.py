"""Tests for browser/anti_detect.py — fast mode, delay logic, Bezier math."""

import asyncio
import random
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from auto_applier.browser import anti_detect


@pytest.fixture(autouse=True)
def reset_fast_mode():
    """Reset fast mode after each test."""
    anti_detect.set_fast_mode(False)
    yield
    anti_detect.set_fast_mode(False)


class TestFastMode:
    def test_default_off(self):
        assert anti_detect.is_fast_mode() is False

    def test_enable(self):
        anti_detect.set_fast_mode(True)
        assert anti_detect.is_fast_mode() is True

    def test_disable(self):
        anti_detect.set_fast_mode(True)
        anti_detect.set_fast_mode(False)
        assert anti_detect.is_fast_mode() is False


class TestRandomDelay:
    def test_normal_mode_respects_bounds(self):
        """In normal mode, delay is >= min_sec (ignoring distraction)."""
        async def run():
            # Use fixed random to avoid distraction multiplier
            with patch("auto_applier.browser.anti_detect.random") as mock_rng:
                mock_rng.random.return_value = 0.5  # No distraction (> 0.15)
                mock_rng.uniform.return_value = 1.0
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await anti_detect.random_delay(min_sec=1.0, max_sec=2.0)
                    mock_sleep.assert_called_once_with(1.0)

        asyncio.run(run())

    def test_fast_mode_scales_down(self):
        """Fast mode divides bounds by 4."""
        anti_detect.set_fast_mode(True)

        async def run():
            with patch("auto_applier.browser.anti_detect.random") as mock_rng:
                mock_rng.uniform.return_value = 0.5
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await anti_detect.random_delay(min_sec=4.0, max_sec=8.0)
                    # 4.0/4 = 1.0, 8.0/4 = 2.0; uniform returns 0.5
                    mock_sleep.assert_called_once_with(0.5)

        asyncio.run(run())

    def test_distraction_multiplier_triggers(self):
        """15% chance of distraction multiplier in normal mode."""
        async def run():
            with patch("auto_applier.browser.anti_detect.random") as mock_rng:
                mock_rng.random.return_value = 0.05  # < 0.15 -> triggers distraction
                mock_rng.uniform.side_effect = [3.0, 2.0]  # multiplier=3.0, then delay=2.0
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    await anti_detect.random_delay(min_sec=1.0, max_sec=2.0)
                    mock_sleep.assert_called_once_with(2.0)

        asyncio.run(run())


class TestSimulateOrganicBehavior:
    def test_fast_mode_skips(self):
        """Fast mode returns immediately without any actions."""
        anti_detect.set_fast_mode(True)
        page = MagicMock()

        async def run():
            await anti_detect.simulate_organic_behavior(page)

        asyncio.run(run())
        # Page should not have been touched
        page.mouse.move.assert_not_called()
        page.mouse.wheel.assert_not_called()


class TestReadingPause:
    def test_fast_mode_short_pause(self):
        anti_detect.set_fast_mode(True)

        async def run():
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await anti_detect.reading_pause(MagicMock())
                delay = mock_sleep.call_args[0][0]
                assert 0.5 <= delay <= 1.5

        asyncio.run(run())

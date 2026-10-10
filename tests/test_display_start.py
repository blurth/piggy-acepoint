from unittest.mock import Mock

import pytest

from acepoint.displays.base import CompositeDisplay


def test_failed_backend_is_closed_and_removed():
    broken = Mock()
    broken.start.side_effect = OSError("SPI unavailable")
    healthy = Mock()
    display = CompositeDisplay([broken, healthy])
    display.start()
    display.update("telemetry")
    broken.close.assert_called_once()
    broken.update.assert_not_called()
    healthy.update.assert_called_once_with("telemetry")


def test_all_failed_backends_report_startup_failure():
    broken = Mock()
    broken.start.side_effect = ImportError("luma missing")
    with pytest.raises(RuntimeError, match="显示后端"):
        CompositeDisplay([broken]).start()

"""§18 dedupe: staleness on_created / on_modified merged.

T-0017-staleness: on_created and on_modified had identical bodies.
After the refactor they both delegate to a single _handle_event method
(or on_modified replaces both).  These tests verify:

  1. Both on_created and on_modified call _handle_file_change.
  2. Neither silently no-ops for non-directory events.
  3. Directory events are still ignored by both.
"""

from unittest.mock import MagicMock

import pytest

from yadgar.staleness import StalenessDetector, _FileChangeHandler


@pytest.fixture()
def mock_detector():
    storage = MagicMock()
    from yadgar.config import Settings

    settings = Settings()
    detector = StalenessDetector(storage=storage, settings=settings)
    detector._handle_file_change = MagicMock()
    return detector


@pytest.fixture()
def handler(mock_detector):
    return _FileChangeHandler(detector=mock_detector)


def _make_event(path: str, is_dir: bool = False):
    evt = MagicMock()
    evt.src_path = path
    evt.is_directory = is_dir
    return evt


def test_on_modified_calls_handle_file_change(handler, mock_detector):
    evt = _make_event("/some/file.py")
    handler.on_modified(evt)
    mock_detector._handle_file_change.assert_called_once_with("/some/file.py")


def test_on_created_calls_handle_file_change(handler, mock_detector):
    evt = _make_event("/some/new_file.py")
    handler.on_created(evt)
    mock_detector._handle_file_change.assert_called_once_with("/some/new_file.py")


def test_on_modified_ignores_directories(handler, mock_detector):
    evt = _make_event("/some/dir/", is_dir=True)
    handler.on_modified(evt)
    mock_detector._handle_file_change.assert_not_called()


def test_on_created_ignores_directories(handler, mock_detector):
    evt = _make_event("/some/dir/", is_dir=True)
    handler.on_created(evt)
    mock_detector._handle_file_change.assert_not_called()


def test_both_trigger_same_handler_path(mock_detector):
    """on_created and on_modified must produce identical side-effects."""
    handler1 = _FileChangeHandler(detector=mock_detector)
    handler2 = _FileChangeHandler(detector=mock_detector)

    evt = _make_event("/shared/path.py")

    handler1.on_modified(evt)
    handler2.on_created(evt)

    assert mock_detector._handle_file_change.call_count == 2
    calls = mock_detector._handle_file_change.call_args_list
    assert calls[0] == calls[1], "Both handlers must call _handle_file_change with same args"

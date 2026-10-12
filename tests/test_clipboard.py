"""Clipboard writes keep only the latest dictation in Maccy's history (#629).

Every test writes to a private, uniquely named pasteboard patched in as the
general one, so the real clipboard and Maccy's history are never touched."""
import threading
from unittest import mock

import pytest
from AppKit import NSPasteboard, NSPasteboardTypeString

import main


@pytest.fixture
def pasteboard():
    pb = NSPasteboard.pasteboardWithUniqueName()
    # PyObjC classes refuse attribute patches, so swap the whole name in main.
    fake = mock.Mock(generalPasteboard=mock.Mock(return_value=pb))
    with mock.patch.object(main, "NSPasteboard", fake), \
         mock.patch.object(main, "_last_clip_change_count", None):
        yield pb
    pb.releaseGlobally()


def _marker(pb):
    return pb.stringForType_(main.MACCY_MODIFIED_TYPE)


def _in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join(10)
    assert not t.is_alive()


def test_first_write_replaces_nothing(pasteboard):
    main._copy_to_clipboard("first")
    assert pasteboard.stringForType_(NSPasteboardTypeString) == "first"
    assert _marker(pasteboard) is None


def test_each_write_names_the_previous_one(pasteboard):
    main._copy_to_clipboard("first")
    first = pasteboard.changeCount()
    main._copy_to_clipboard("second")
    assert pasteboard.stringForType_(NSPasteboardTypeString) == "second"
    assert _marker(pasteboard) == str(first)
    second = pasteboard.changeCount()
    main._copy_to_clipboard("third")
    assert _marker(pasteboard) == str(second)


def test_write_from_worker_thread_runs_on_main(pasteboard):
    # Stand in for the run loop: run the hopped call synchronously.
    with mock.patch.object(main.AppHelper, "callAfter", side_effect=lambda fn: fn()), \
         mock.patch.object(main, "_pbcopy") as pbcopy:
        _in_thread(lambda: main._copy_to_clipboard("from worker"))
    assert pasteboard.stringForType_(NSPasteboardTypeString) == "from worker"
    pbcopy.assert_not_called()


def test_wedged_main_thread_falls_back_to_pbcopy(pasteboard):
    queued = []
    with mock.patch.object(main.AppHelper, "callAfter", side_effect=queued.append), \
         mock.patch.object(main, "CLIPBOARD_MAIN_THREAD_TIMEOUT", 0.05), \
         mock.patch.object(main, "log"), \
         mock.patch.object(main, "_pbcopy") as pbcopy:
        _in_thread(lambda: main._copy_to_clipboard("wedged"))
    pbcopy.assert_called_once_with("wedged")
    # The run loop wakes up late: it must not write the text a second time.
    queued[0]()
    assert pasteboard.stringForType_(NSPasteboardTypeString) is None


def test_failed_pasteboard_write_falls_back_to_pbcopy(pasteboard):
    with mock.patch.object(main.AppHelper, "callAfter", side_effect=lambda fn: fn()), \
         mock.patch.object(main, "_write_pasteboard", side_effect=RuntimeError("boom")), \
         mock.patch.object(main, "log"), \
         mock.patch.object(main, "_pbcopy") as pbcopy:
        _in_thread(lambda: main._copy_to_clipboard("still lands"))
    pbcopy.assert_called_once_with("still lands")

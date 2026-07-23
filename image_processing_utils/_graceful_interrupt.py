"""Cooperative Ctrl-C / SIGTERM handling for batch file-processing scripts."""

import logging
import signal

logger = logging.getLogger(__name__)

_interrupt_requested = False
_handler_installed = False


def install_graceful_interrupt_handler():
	global _handler_installed
	if _handler_installed:
		return
	signal.signal(signal.SIGINT, _handle_interrupt)
	if hasattr(signal, "SIGTERM"):
		signal.signal(signal.SIGTERM, _handle_interrupt)
	_handler_installed = True


def _handle_interrupt(signum, frame):
	del signum, frame
	global _interrupt_requested
	if _interrupt_requested:
		logger.error("Second interrupt received; exiting immediately.")
		signal.signal(signal.SIGINT, signal.SIG_DFL)
		raise KeyboardInterrupt
	_interrupt_requested = True
	logger.warning("Interrupt received; finishing current file, then stopping.")


def interrupt_requested():
	return _interrupt_requested


def abort_if_interrupt_requested(*, completed_item=None):
	if not _interrupt_requested:
		return
	if completed_item:
		logger.error("Aborted by user after completing %s.", completed_item)
	else:
		logger.error("Aborted by user.")
	raise SystemExit(130)


def reset_graceful_interrupt_for_tests():
	global _interrupt_requested, _handler_installed
	_interrupt_requested = False
	_handler_installed = False


def request_interrupt_for_tests():
	global _interrupt_requested
	_interrupt_requested = True

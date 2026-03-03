"""
Custom logging levels for Clowder.

Standard Python levels (for reference):
    CRITICAL = 50
    ERROR    = 40
    WARNING  = 30
    INFO     = 20
    DEBUG    = 10

Added levels:
    MODEL    =  8  — raw stdout/stderr produced by a model call
    TRACE    =  5  — very fine-grained internal tracing

Import this module anywhere to register the levels.  After import,
``logging.getLogger(...).model(msg)`` and ``.trace(msg)`` are available.
"""

import logging

TRACE = 5
MODEL = 8

logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(MODEL, "MODEL")


def _trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


def _model(self, message, *args, **kwargs):
    if self.isEnabledFor(MODEL):
        self._log(MODEL, message, args, **kwargs)


logging.Logger.trace = _trace  # type: ignore[attr-defined]
logging.Logger.model = _model  # type: ignore[attr-defined]

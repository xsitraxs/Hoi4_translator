"""Exceptions shared by the translation layer and the file pipeline."""


class TranslationCancelled(Exception):
    """Raised when the user stops the active file translation."""


class TranslationFailed(Exception):
    """Raised when every configured translation engine fails for a string."""


class TranslationValidationFailed(TranslationFailed):
    """Every engine answered, but no answer can be written to the game.

    Raised instead of returning a value that lost a ``$STATE$`` marker, gained
    an unescaped quote or broke the one-line format: such a value looks like a
    successful translation and only fails later, in the game.

    The pipeline decides what a line like that means for the file: refuse the
    whole file (the strict mode) or keep the English text for that line only.
    """

    def __init__(self, message: str, *, source: str = "", candidate: str = "",
                 issues=(), attempts=()):
        super().__init__(message)
        self.source = source
        # The last answer we got. It is not a usable translation; it is handed
        # back only so the pipeline can validate the row like any other and
        # decide what a broken line means for the file.
        self.candidate = candidate
        self.issues = list(issues)
        self.attempts = list(attempts)

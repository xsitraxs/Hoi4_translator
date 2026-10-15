"""Exceptions shared by the translation layer and the file pipeline."""


class TranslationCancelled(Exception):
    """Raised when the user stops the active file translation."""


class TranslationFailed(Exception):
    """Raised when every configured translation engine fails for a string."""

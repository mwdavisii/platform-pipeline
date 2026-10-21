"""Exit codes and the error the CLI turns into a builder-result."""

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_NEEDS_INPUT = 3
EXIT_DRIFT = 4


class BuilderError(Exception):
    def __init__(self, exit_code: int, errors=None, questions=None, warnings=None):
        self.exit_code = exit_code
        self.errors = list(errors or [])
        self.questions = list(questions or [])
        self.warnings = list(warnings or [])
        super().__init__("; ".join(self.errors + self.questions))

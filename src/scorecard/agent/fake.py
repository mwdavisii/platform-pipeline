"""A fake model client that answers like a careful reviewer would, from a fixed answer key. No network.

Used by the normal test suite and by the golden set's fake mode (AC-9: a fixed fake gives a fixed scorecard). On its
first turn in a batch it reads the files it is told to; on the next it submits an answer for every requirement listed
in the batch message. Everything it was sent is kept in `sent`, so tests can check what reached the model.
"""

import copy
import json
import re

from .gateway import Completion

_ASKED = re.compile(r"^- (v5\.0\.0-[0-9.]+) \(L[123]\): ", re.M)


def tool_call(name: str, arguments, call_id: str = "call-1") -> dict:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": text}}]}


class OracleModel:
    """answers: {asvs_id: result fields}; default: the result fields for any other requirement; findings: agent-batch/v1
    finding drafts. A finding is sent with the batch whose results name its ref, or with the first batch if no answer names it."""

    def __init__(self, *, default: dict, answers: dict | None = None, findings: list | None = None, reads=("*",), tokens: int = 1000, truncated: bool = False):
        self.default, self.answers, self.findings = default, answers or {}, findings or []
        self.reads, self.tokens, self.truncated = list(reads), tokens, truncated
        self.sent: list[list[dict]] = []
        self._submitted = 0

    def _result(self, asvs_id: str) -> dict:
        fields = {"confidence": "high", "justification": "fixture answer", "evidence": [], "finding_refs": [],
                  **self.default, **self.answers.get(asvs_id, {})}
        return {"asvs_id": asvs_id, **copy.deepcopy(fields)}

    def _drafts(self, results: list[dict]) -> list[dict]:
        named = {ref for r in results for ref in r["finding_refs"]}
        bound = {ref for a in self.answers.values() for ref in a.get("finding_refs", [])}
        return [f for f in self.findings if f["ref"] in named or (self._submitted == 0 and f["ref"] not in bound)]

    def complete(self, messages, tools, *, max_tokens, tool_choice=None):
        self.sent.append(copy.deepcopy(messages))
        if len(messages) == 2 and self.reads:  # the batch's first turn: read before answering
            calls = [{"id": f"read-{n}", "type": "function",
                      "function": {"name": "list_files" if path == "*" else "read_file",
                                   "arguments": json.dumps({"glob": "*"} if path == "*" else {"path": path})}}
                     for n, path in enumerate(self.reads)]
            return Completion(message={"role": "assistant", "content": None, "tool_calls": calls}, tokens=self.tokens, truncated=self.truncated)
        results = [self._result(i) for i in _ASKED.findall(messages[1]["content"])]
        drafts = self._drafts(results)
        self._submitted += 1
        return Completion(message=tool_call("submit_assessment", {"results": results, "findings": drafts}, "submit-1"), tokens=self.tokens, truncated=self.truncated)

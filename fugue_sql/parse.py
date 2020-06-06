from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener
from fugue_sql.antlr import FugueSQLLexer, FugueSQLParser
from fugue_sql.exceptions import FugueSQLSyntaxError
from antlr4.tree.Tree import TerminalNode, Tree, Token
from typing import List, Iterable


class FugueSQL(object):
    def __init__(self, code: str, rule: str, ignore_case: bool = False):
        self._rule = rule
        self._raw_code = code
        self._code = _to_cased_code(code, rule) if ignore_case else code
        self._tree = _to_tree(self._code, self._rule, False)

    @property
    def raw_code(self):  # pragma: no cover
        return self._raw_code

    @property
    def code(self):  # pragma: no cover
        return self._code

    @property
    def tree(self) -> Tree:  # pragma: no cover
        return self._tree


def _to_tree(code: str, rule: str, all_upper_case: bool) -> Tree:
    input_stream = InputStream(code)
    lexer = FugueSQLLexer(input_stream)
    stream = CommonTokenStream(lexer)
    parser = FugueSQLParser(stream)
    parser._all_upper_case = all_upper_case
    parser.addErrorListener(_ErrorListener(code.splitlines()))
    return getattr(parser, rule)()  # validate syntax


def _to_cased_code(code: str, rule: str) -> str:
    tokens = [
        t for t in _to_tokens(_to_tree(code.upper(), rule, True)) if _is_keyword(t)
    ]
    start = 0
    cased_code: List[str] = []
    for t in tokens:
        if t.start > start:
            cased_code.append(code[start : t.start])
        cased_code.append(code[t.start : t.stop + 1].upper())
        start = t.stop + 1
    if start < len(code):
        cased_code.append(code[start:])
    return "".join(cased_code)


def _to_tokens(node: Tree) -> Iterable[Token]:
    if isinstance(node, TerminalNode):
        yield node.getSymbol()
    else:
        for i in range(node.getChildCount()):
            for x in _to_tokens(node.getChild(i)):
                yield x


def _is_keyword(token: Token):
    if not hasattr(FugueSQLParser, token.text):
        return False
    return getattr(FugueSQLParser, token.text) == token.type


class _ErrorListener(ErrorListener):
    def __init__(self, lines: List[str]):
        super().__init__()
        self._lines = lines

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        raise FugueSQLSyntaxError(f"{msg}\nline {line}: {self._lines[line - 1]}")

    def reportAmbiguity(
        self, recognizer, dfa, startIndex, stopIndex, exact, ambigAlts, configs
    ):  # pragma: no cover
        pass

    def reportAttemptingFullContext(
        self, recognizer, dfa, startIndex, stopIndex, conflictingAlts, configs
    ):  # pragma: no cover
        pass

    def reportContextSensitivity(
        self, recognizer, dfa, startIndex, stopIndex, prediction, configs
    ):  # pragma: no cover
        pass

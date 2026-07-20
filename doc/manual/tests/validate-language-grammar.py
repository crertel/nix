#!/usr/bin/env python3
"""Validate the machine-readable Nix language grammar.

This intentionally uses only the Python standard library.  It reads the two
marked EBNF blocks in ``source/language/grammar.md``, parses and audits the
EBNF, tokenizes Nix source text, and recognizes the resulting token stream
with an Earley parser generated from the documented syntactic grammar.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import concurrent.futures
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Iterator, Sequence, TypeAlias


SCRIPT = Path(__file__).resolve()
REPOSITORY = SCRIPT.parents[3]
DEFAULT_GRAMMAR = SCRIPT.parent.parent / "source" / "language" / "grammar.md"
DEFAULT_CORPUS = REPOSITORY / "tests" / "functional" / "lang"


class ValidationError(Exception):
    """A user-facing validation failure."""


@dataclasses.dataclass(frozen=True)
class SourceLocation:
    path: Path
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


@dataclasses.dataclass(frozen=True)
class EbnfToken:
    kind: str
    value: str
    location: SourceLocation


@dataclasses.dataclass(frozen=True)
class Empty:
    pass


@dataclasses.dataclass(frozen=True)
class Reference:
    name: str
    location: SourceLocation


@dataclasses.dataclass(frozen=True)
class Literal:
    value: str
    location: SourceLocation


@dataclasses.dataclass(frozen=True)
class Special:
    value: str
    location: SourceLocation


@dataclasses.dataclass(frozen=True)
class SequenceNode:
    items: tuple[EbnfNode, ...]


@dataclasses.dataclass(frozen=True)
class Alternative:
    items: tuple[EbnfNode, ...]


@dataclasses.dataclass(frozen=True)
class OptionalNode:
    item: EbnfNode
    location: SourceLocation


@dataclasses.dataclass(frozen=True)
class Repetition:
    item: EbnfNode
    location: SourceLocation


EbnfNode: TypeAlias = (
    Empty
    | Reference
    | Literal
    | Special
    | SequenceNode
    | Alternative
    | OptionalNode
    | Repetition
)


@dataclasses.dataclass(frozen=True)
class Production:
    name: str
    expression: EbnfNode
    location: SourceLocation


@dataclasses.dataclass(frozen=True)
class ExtractedBlock:
    text: str
    first_line: int


def extract_marked_ebnf(markdown: Path, marker: str) -> ExtractedBlock:
    """Extract the sole EBNF fence immediately following an HTML marker."""

    try:
        text = markdown.read_text(encoding="utf-8")
    except OSError as error:
        raise ValidationError(f"{markdown}: cannot read grammar: {error}") from error

    marker_text = f"<!-- {marker} -->"
    marker_offsets = [match.start() for match in re.finditer(re.escape(marker_text), text)]
    if len(marker_offsets) != 1:
        raise ValidationError(
            f"{markdown}: expected exactly one {marker_text!r}, found "
            f"{len(marker_offsets)}"
        )

    after_marker = marker_offsets[0] + len(marker_text)
    fence = re.search(r"^[ \t]*```ebnf[ \t]*\r?$", text[after_marker:], re.MULTILINE)
    if fence is None:
        raise ValidationError(f"{markdown}: no ```ebnf fence follows {marker_text}")

    opening_start = after_marker + fence.start()
    opening_end = after_marker + fence.end()
    intervening = text[after_marker:opening_start]
    if re.search(r"^[ \t]*```", intervening, re.MULTILINE):
        raise ValidationError(
            f"{markdown}: a different code fence occurs before the EBNF block "
            f"for {marker_text}"
        )

    content_start = opening_end
    if content_start < len(text) and text[content_start] == "\r":
        content_start += 1
    if content_start < len(text) and text[content_start] == "\n":
        content_start += 1

    closing = re.search(r"^[ \t]*```[ \t]*\r?$", text[content_start:], re.MULTILINE)
    if closing is None:
        line = text.count("\n", 0, opening_start) + 1
        raise ValidationError(f"{markdown}:{line}: unterminated EBNF fence")

    content_end = content_start + closing.start()
    first_line = text.count("\n", 0, content_start) + 1
    return ExtractedBlock(text[content_start:content_end], first_line)


class EbnfLexer:
    """Lexer for the small ISO-style EBNF dialect used by the manual."""

    _identifier = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")

    def __init__(self, path: Path, block: ExtractedBlock):
        self.path = path
        self.text = block.text
        self.first_line = block.first_line
        self.offset = 0
        self.line = block.first_line
        self.column = 1

    def location(self) -> SourceLocation:
        return SourceLocation(self.path, self.line, self.column)

    def advance(self, count: int) -> str:
        value = self.text[self.offset : self.offset + count]
        self.offset += count
        for char in value:
            if char == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
        return value

    def skip_space_and_comments(self) -> None:
        while self.offset < len(self.text):
            if self.text[self.offset].isspace():
                self.advance(1)
                continue
            if self.text.startswith("(*", self.offset):
                start = self.location()
                self.advance(2)
                depth = 1
                while depth:
                    if self.offset >= len(self.text):
                        raise ValidationError(f"{start}: unterminated EBNF comment")
                    if self.text.startswith("(*", self.offset):
                        self.advance(2)
                        depth += 1
                    elif self.text.startswith("*)", self.offset):
                        self.advance(2)
                        depth -= 1
                    else:
                        self.advance(1)
                continue
            return

    def quoted(self, quote: str, start: SourceLocation) -> EbnfToken:
        self.advance(1)
        value: list[str] = []
        while self.offset < len(self.text):
            char = self.text[self.offset]
            if char == quote:
                if self.offset + 1 < len(self.text) and self.text[self.offset + 1] == quote:
                    self.advance(2)
                    value.append(quote)
                    continue
                self.advance(1)
                return EbnfToken("literal", "".join(value), start)
            if char in "\r\n":
                raise ValidationError(f"{start}: newline in quoted EBNF literal")
            value.append(self.advance(1))
        raise ValidationError(f"{start}: unterminated quoted EBNF literal")

    def special(self, start: SourceLocation) -> EbnfToken:
        self.advance(1)
        end = self.text.find("?", self.offset)
        if end < 0:
            raise ValidationError(f"{start}: unterminated EBNF special sequence")
        value = self.text[self.offset:end].strip()
        self.advance(end - self.offset + 1)
        if not value:
            raise ValidationError(f"{start}: empty EBNF special sequence")
        return EbnfToken("special", value, start)

    def tokens(self) -> Iterator[EbnfToken]:
        symbols = "=;|,()[]{}"
        while True:
            self.skip_space_and_comments()
            if self.offset >= len(self.text):
                yield EbnfToken("eof", "", self.location())
                return

            start = self.location()
            char = self.text[self.offset]
            if char in symbols:
                yield EbnfToken(char, self.advance(1), start)
                continue
            if char in "\"'":
                yield self.quoted(char, start)
                continue
            if char == "?":
                yield self.special(start)
                continue

            match = self._identifier.match(self.text, self.offset)
            if match is not None:
                value = match.group(0)
                self.advance(len(value))
                yield EbnfToken("identifier", value, start)
                continue

            raise ValidationError(
                f"{start}: unexpected character {char!r} in EBNF block"
            )


class EbnfParser:
    def __init__(self, tokens: Iterable[EbnfToken]):
        self.tokens = iter(tokens)
        self.current = next(self.tokens)

    def advance(self) -> EbnfToken:
        previous = self.current
        self.current = next(self.tokens)
        return previous

    def expect(self, kind: str) -> EbnfToken:
        if self.current.kind != kind:
            raise ValidationError(
                f"{self.current.location}: expected {kind!r}, found "
                f"{self.current.value!r}"
            )
        return self.advance()

    def parse(self) -> dict[str, Production]:
        productions: dict[str, Production] = {}
        while self.current.kind != "eof":
            name = self.expect("identifier")
            self.expect("=")
            expression = self.parse_alternative({";"})
            self.expect(";")
            if name.value in productions:
                first = productions[name.value].location
                raise ValidationError(
                    f"{name.location}: duplicate production {name.value!r}; "
                    f"first defined at {first}"
                )
            productions[name.value] = Production(
                name.value, expression, name.location
            )
        if not productions:
            raise ValidationError(f"{self.current.location}: empty EBNF block")
        return productions

    def parse_alternative(self, terminators: set[str]) -> EbnfNode:
        items = [self.parse_sequence(terminators | {"|"})]
        while self.current.kind == "|":
            self.advance()
            items.append(self.parse_sequence(terminators | {"|"}))
        if len(items) == 1:
            return items[0]
        return Alternative(tuple(items))

    def parse_sequence(self, terminators: set[str]) -> EbnfNode:
        if self.current.kind in terminators:
            return Empty()
        items = [self.parse_primary()]
        while self.current.kind == ",":
            comma = self.advance()
            if self.current.kind in terminators or self.current.kind == ",":
                raise ValidationError(
                    f"{comma.location}: comma must be followed by an EBNF item"
                )
            items.append(self.parse_primary())
        if self.current.kind not in terminators:
            raise ValidationError(
                f"{self.current.location}: expected ',' or an expression "
                f"terminator, found {self.current.value!r}"
            )
        if len(items) == 1:
            return items[0]
        return SequenceNode(tuple(items))

    def parse_primary(self) -> EbnfNode:
        token = self.current
        if token.kind == "identifier":
            self.advance()
            return Reference(token.value, token.location)
        if token.kind == "literal":
            self.advance()
            return Literal(token.value, token.location)
        if token.kind == "special":
            self.advance()
            return Special(token.value, token.location)
        if token.kind == "(":
            self.advance()
            expression = self.parse_alternative({")"})
            self.expect(")")
            return expression
        if token.kind == "[":
            self.advance()
            expression = self.parse_alternative({"]"})
            self.expect("]")
            return OptionalNode(expression, token.location)
        if token.kind == "{":
            self.advance()
            expression = self.parse_alternative({"}"})
            self.expect("}")
            return Repetition(expression, token.location)
        raise ValidationError(
            f"{token.location}: expected an EBNF item, found {token.value!r}"
        )


def parse_ebnf(path: Path, marker: str) -> dict[str, Production]:
    block = extract_marked_ebnf(path, marker)
    return EbnfParser(EbnfLexer(path, block).tokens()).parse()


def walk(node: EbnfNode) -> Iterator[EbnfNode]:
    yield node
    if isinstance(node, (SequenceNode, Alternative)):
        for item in node.items:
            yield from walk(item)
    elif isinstance(node, (OptionalNode, Repetition)):
        yield from walk(node.item)


def referenced_names(node: EbnfNode) -> set[str]:
    return {
        item.name
        for item in walk(node)
        if isinstance(item, Reference)
    }


def canonical_ebnf_node(node: EbnfNode) -> object:
    """Serialize an EBNF node without source-formatting or location details."""

    if isinstance(node, Empty):
        return ["empty"]
    if isinstance(node, Reference):
        return ["reference", node.name]
    if isinstance(node, Literal):
        return ["literal", node.value]
    if isinstance(node, Special):
        return ["special", " ".join(node.value.split())]
    if isinstance(node, SequenceNode):
        return ["sequence", [canonical_ebnf_node(item) for item in node.items]]
    if isinstance(node, Alternative):
        return ["alternative", [canonical_ebnf_node(item) for item in node.items]]
    if isinstance(node, OptionalNode):
        return ["optional", canonical_ebnf_node(node.item)]
    if isinstance(node, Repetition):
        return ["repetition", canonical_ebnf_node(node.item)]
    raise AssertionError(node)


def ebnf_contract_digest(productions: dict[str, Production]) -> str:
    """Hash the semantic AST used as the handwritten scanner's contract."""

    canonical = [
        [name, canonical_ebnf_node(productions[name].expression)]
        for name in sorted(productions)
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_graph(
    label: str,
    productions: dict[str, Production],
    roots: Sequence[str],
    *,
    allowed_external: set[str] | None = None,
) -> None:
    allowed_external = allowed_external or set()
    missing_roots = set(roots) - productions.keys()
    if missing_roots:
        raise ValidationError(
            f"{label} grammar lacks root production(s): "
            f"{', '.join(sorted(missing_roots))}"
        )

    for production in productions.values():
        for node in walk(production.expression):
            if (
                isinstance(node, Reference)
                and node.name not in productions
                and node.name not in allowed_external
            ):
                raise ValidationError(
                    f"{node.location}: undefined {label} production "
                    f"{node.name!r}"
                )

    reachable: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in reachable or name not in productions:
            continue
        reachable.add(name)
        pending.extend(referenced_names(productions[name].expression))

    unreachable = productions.keys() - reachable
    if unreachable:
        details = ", ".join(
            f"{name} ({productions[name].location})" for name in sorted(unreachable)
        )
        raise ValidationError(f"unreachable {label} production(s): {details}")

    productive: set[str] = set()

    def node_is_productive(node: EbnfNode) -> bool:
        if isinstance(node, Empty):
            return True
        if isinstance(node, (Literal, Special)):
            return True
        if isinstance(node, Reference):
            return node.name in allowed_external or node.name in productive
        if isinstance(node, (OptionalNode, Repetition)):
            return True
        if isinstance(node, SequenceNode):
            return all(node_is_productive(item) for item in node.items)
        if isinstance(node, Alternative):
            return any(node_is_productive(item) for item in node.items)
        raise AssertionError(node)

    changed = True
    while changed:
        changed = False
        for name, production in productions.items():
            if name not in productive and node_is_productive(production.expression):
                productive.add(name)
                changed = True

    unproductive = productions.keys() - productive
    if unproductive:
        raise ValidationError(
            f"unproductive {label} production(s): "
            f"{', '.join(sorted(unproductive))}"
        )

    nullable: set[str] = set()

    def node_is_nullable(node: EbnfNode) -> bool:
        if isinstance(node, Empty):
            return True
        if isinstance(node, Literal):
            return node.value == ""
        if isinstance(node, Special):
            # Specials describe scanner predicates, some of which are
            # zero-width.  Their precise width is intentionally opaque.
            return False
        if isinstance(node, Reference):
            return node.name in nullable
        if isinstance(node, (OptionalNode, Repetition)):
            return True
        if isinstance(node, SequenceNode):
            return all(node_is_nullable(item) for item in node.items)
        if isinstance(node, Alternative):
            return any(node_is_nullable(item) for item in node.items)
        raise AssertionError(node)

    changed = True
    while changed:
        changed = False
        for name, production in productions.items():
            if name not in nullable and node_is_nullable(production.expression):
                nullable.add(name)
                changed = True

    for production in productions.values():
        for node in walk(production.expression):
            if isinstance(node, Repetition) and node_is_nullable(node.item):
                raise ValidationError(
                    f"{node.location}: repetition body in {production.name!r} "
                    "can match an empty sequence"
                )


def direct_reference_alternatives(production: Production) -> set[str]:
    expression = production.expression
    alternatives = expression.items if isinstance(expression, Alternative) else (expression,)
    result: set[str] = set()
    for item in alternatives:
        if not isinstance(item, Reference):
            raise ValidationError(
                f"{production.location}: {production.name!r} must contain only "
                "direct production-reference alternatives"
            )
        result.add(item.name)
    return result


def finite_literal_language(
    productions: dict[str, Production],
    root: str,
) -> set[str]:
    """Expand a finite, literal-only production such as ``fixed-token``."""

    cache: dict[str, set[str]] = {}
    active: set[str] = set()

    def expand_production(name: str) -> set[str]:
        if name in cache:
            return cache[name]
        if name in active:
            raise ValidationError(
                f"{productions[name].location}: recursive production {name!r} "
                "cannot define a finite token inventory"
            )
        if name not in productions:
            raise ValidationError(
                f"finite token inventory references undefined production {name!r}"
            )
        active.add(name)
        result = expand_node(productions[name].expression)
        active.remove(name)
        cache[name] = result
        return result

    def concatenate(parts: Sequence[set[str]], location: SourceLocation) -> set[str]:
        result = {""}
        for part in parts:
            result = {left + right for left in result for right in part}
            if len(result) > 1024:
                raise ValidationError(
                    f"{location}: finite token inventory expands beyond 1024 items"
                )
        return result

    def expand_node(node: EbnfNode) -> set[str]:
        if isinstance(node, Empty):
            return {""}
        if isinstance(node, Literal):
            return {node.value}
        if isinstance(node, Reference):
            return expand_production(node.name)
        if isinstance(node, SequenceNode):
            return concatenate(
                [expand_node(item) for item in node.items],
                productions[root].location,
            )
        if isinstance(node, Alternative):
            return set().union(*(expand_node(item) for item in node.items))
        if isinstance(node, OptionalNode):
            return {""} | expand_node(node.item)
        if isinstance(node, (Special, Repetition)):
            location = (
                node.location
                if isinstance(node, Repetition)
                else productions[root].location
            )
            raise ValidationError(
                f"{location}: finite token inventory may not contain "
                f"{type(node).__name__}"
            )
        raise AssertionError(node)

    result = expand_production(root)
    if "" in result:
        raise ValidationError(
            f"{productions[root].location}: finite token inventory contains "
            "an empty token"
        )
    return result


EXPECTED_LEXICAL_TOKENS = {
    "identifier",
    "integer",
    "floating-point",
    "double-quote",
    "string-fragment",
    "indented-string-open",
    "indented-string-fragment",
    "indented-string-close",
    "path-start",
    "path-fragment",
    "path-end",
    "lookup-path",
    "uri",
}

# Any semantic change to the lexical EBNF requires an explicit review of the
# stateful NixLexer transcription and regeneration of this normalized-AST
# contract. Formatting, comments, and source locations do not affect the hash.
NIX_LEXER_TRANSCRIPTION_CONTRACT_SHA256 = (
    "34ac7dfc2efb7caf7e03590bc12ac3a5d056ea5fe98aee7e341a0602fd34c797"
)


@dataclasses.dataclass(frozen=True)
class GrammarTerminal:
    category: str
    value: str

    def describe(self) -> str:
        if self.category == "literal":
            return repr(self.value)
        return f"<{self.value}>"


GrammarSymbol: TypeAlias = str | GrammarTerminal


class BnfGrammar:
    def __init__(
        self,
        syntax: dict[str, Production],
        lexical_tokens: set[str],
        start: str,
    ):
        self.syntax = syntax
        self.lexical_tokens = lexical_tokens
        self.start = start
        self.rules: dict[str, list[tuple[GrammarSymbol, ...]]] = collections.defaultdict(list)
        self.synthetic_counter = 0
        self._lower()

    def synthetic(self, hint: str) -> str:
        self.synthetic_counter += 1
        return f"@{hint}:{self.synthetic_counter}"

    def _lower(self) -> None:
        if self.start not in self.syntax:
            raise ValidationError(
                f"syntactic grammar lacks requested start production {self.start!r}"
            )
        for name, production in self.syntax.items():
            self.emit(name, production.expression)

    def emit(self, name: str, node: EbnfNode) -> None:
        if isinstance(node, Alternative):
            for item in node.items:
                self.rules[name].append(self.as_sequence(item))
        else:
            self.rules[name].append(self.as_sequence(node))

    def as_sequence(self, node: EbnfNode) -> tuple[GrammarSymbol, ...]:
        if isinstance(node, Empty):
            return ()
        if isinstance(node, SequenceNode):
            result: list[GrammarSymbol] = []
            for item in node.items:
                result.extend(self.as_sequence(item))
            return tuple(result)
        if isinstance(node, Reference):
            if node.name in self.lexical_tokens:
                return (GrammarTerminal("token", node.name),)
            if node.name not in self.syntax:
                raise ValidationError(
                    f"{node.location}: syntactic grammar may only reference "
                    f"syntactic productions or direct lexical-token alternatives; "
                    f"found {node.name!r}"
                )
            return (node.name,)
        if isinstance(node, Literal):
            if not node.value:
                raise ValidationError(
                    f"{node.location}: empty fixed token in syntactic grammar"
                )
            return (GrammarTerminal("literal", node.value),)
        if isinstance(node, Special):
            raise ValidationError(
                f"{node.location}: opaque special sequences are only supported "
                "in the lexical grammar"
            )
        if isinstance(node, Alternative):
            name = self.synthetic("alternative")
            self.emit(name, node)
            return (name,)
        if isinstance(node, OptionalNode):
            name = self.synthetic("optional")
            self.rules[name].append(())
            self.rules[name].append(self.as_sequence(node.item))
            return (name,)
        if isinstance(node, Repetition):
            name = self.synthetic("repetition")
            body = self.as_sequence(node.item)
            self.rules[name].append(())
            self.rules[name].append(body + (name,))
            return (name,)
        raise AssertionError(node)


@dataclasses.dataclass(frozen=True)
class NixToken:
    kind: str | None
    lexeme: bytes
    offset: int
    line: int
    column: int
    virtual: bool = False

    def describe(self) -> str:
        if self.virtual:
            return f"<{self.kind}>"
        escaped = self.lexeme.decode("utf-8", "backslashreplace")
        return repr(escaped)


class NixLexError(ValidationError):
    pass


class NixLexer:
    """A byte-oriented transcription of the scanner states in lexer.l."""

    SINGLE_CHARACTER_TOKENS = ":@;{}().,=!-<>+*/?[]"
    KEYWORDS = {
        b"if",
        b"then",
        b"else",
        b"assert",
        b"with",
        b"let",
        b"in",
        b"rec",
        b"inherit",
        b"or",
    }
    OPERATORS = (
        b"...",
        b"==",
        b"!=",
        b"<=",
        b">=",
        b"&&",
        b"||",
        b"->",
        b"//",
        b"++",
        b"<|",
        b"|>",
        b"${",
    )
    IDENTIFIER = re.compile(rb"[A-Za-z_][A-Za-z0-9_'\-]*")
    INTEGER = re.compile(rb"[0-9]+")
    FLOAT = re.compile(
        rb"(?:(?:[1-9][0-9]*\.[0-9]*)|(?:0?\.[0-9]+))"
        rb"(?:[Ee][+-]?[0-9]+)?"
    )
    PATH_CHARACTER = rb"A-Za-z0-9._+\-"
    PATH = re.compile(
        rb"[" + PATH_CHARACTER + rb"]*(?:/[" + PATH_CHARACTER + rb"]+)+/?"
    )
    HOME_PATH = re.compile(
        rb"~(?:/[" + PATH_CHARACTER + rb"]+)+/?"
    )
    PATH_SEGMENT_BEFORE_INTERPOLATION = re.compile(
        rb"(?:[" + PATH_CHARACTER + rb"]*/|~/)(?=\$\{)"
    )
    LOOKUP_PATH = re.compile(
        rb"<[" + PATH_CHARACTER + rb"]+(?:/[" + PATH_CHARACTER + rb"]+)*>"
    )
    URI = re.compile(
        rb"[A-Za-z][A-Za-z0-9+\-.]*:"
        rb"[A-Za-z0-9%/?:@&=+$,\-_.!~*']+"
    )
    PATH_SEGMENT = re.compile(rb"[" + PATH_CHARACTER + rb"]*/")
    PATH_CHARACTERS = re.compile(rb"[" + PATH_CHARACTER + rb"]+")

    def __init__(self, data: bytes, source: str):
        self.data = data
        self.source = source
        self.offset = 0
        self.states = ["default"]
        self.line_starts = [0]
        index = 0
        while index < len(data):
            if data[index] == 0x0D:
                if index + 1 < len(data) and data[index + 1] == 0x0A:
                    index += 1
                self.line_starts.append(index + 1)
            elif data[index] == 0x0A:
                self.line_starts.append(index + 1)
            index += 1

    def position(self, offset: int | None = None) -> tuple[int, int]:
        offset = self.offset if offset is None else offset
        line_index = bisect.bisect_right(self.line_starts, offset) - 1
        return line_index + 1, offset - self.line_starts[line_index] + 1

    def error(self, message: str) -> NixLexError:
        line, column = self.position()
        return NixLexError(f"{self.source}:{line}:{column}: {message}")

    def token(
        self,
        kind: str | None,
        start: int,
        end: int,
        *,
        virtual: bool = False,
    ) -> NixToken:
        line, column = self.position(start)
        return NixToken(
            kind,
            self.data[start:end],
            start,
            line,
            column,
            virtual,
        )

    def consume_token(self, kind: str | None, length: int) -> NixToken:
        start = self.offset
        self.offset += length
        return self.token(kind, start, self.offset)

    def match_length(self, pattern: re.Pattern[bytes]) -> int:
        match = pattern.match(self.data, self.offset)
        return 0 if match is None else match.end() - self.offset

    def skip_trivia(self) -> bool:
        start = self.offset
        while self.offset < len(self.data) and self.data[self.offset] in b" \t\r\n":
            self.offset += 1
        if self.offset != start:
            return True

        if self.data.startswith(b"#", self.offset):
            self.offset += 1
            while self.offset < len(self.data) and self.data[self.offset] not in b"\r\n":
                self.offset += 1
            return True

        if self.data.startswith(b"/*", self.offset):
            end = self.data.find(b"*/", self.offset + 2)
            if end < 0:
                raise self.error("unterminated block comment")
            self.offset = end + 2
            return True
        return False

    def default_token(self) -> NixToken | None:
        while self.skip_trivia():
            pass
        if self.offset >= len(self.data):
            return None

        # Flex returns a raw NUL as token number zero, which Bison treats as
        # EOF.  Preserve that observable byte-level edge.
        if self.data[self.offset] == 0:
            self.offset = len(self.data)
            return None

        if self.data.startswith(b"''", self.offset):
            length = 2
            cursor = self.offset + 2
            while cursor < len(self.data) and self.data[cursor] == 0x20:
                cursor += 1
            if cursor < len(self.data) and self.data[cursor] == 0x0A:
                length = cursor - self.offset + 1
            result = self.consume_token("indented-string-open", length)
            self.states.append("indented-string")
            return result

        if self.data[self.offset] == 0x22:
            result = self.consume_token("double-quote", 1)
            self.states.append("double-string")
            return result

        candidates: list[tuple[int, int, str | None]] = []

        identifier_length = self.match_length(self.IDENTIFIER)
        if identifier_length:
            word = self.data[self.offset : self.offset + identifier_length]
            if word in self.KEYWORDS:
                candidates.append((identifier_length, 0, None))
            else:
                candidates.append((identifier_length, 2, "identifier"))

        for operator in self.OPERATORS:
            if self.data.startswith(operator, self.offset):
                candidates.append((len(operator), 1, None))

        integer_length = self.match_length(self.INTEGER)
        if integer_length:
            candidates.append((integer_length, 3, "integer"))

        float_length = self.match_length(self.FLOAT)
        if float_length:
            candidates.append((float_length, 4, "floating-point"))

        path_interpolation_length = self.match_length(
            self.PATH_SEGMENT_BEFORE_INTERPOLATION
        )
        if path_interpolation_length:
            candidates.append((path_interpolation_length, 5, "path-start"))

        path_length = self.match_length(self.PATH)
        if path_length:
            candidates.append((path_length, 6, "path-start"))

        home_path_length = self.match_length(self.HOME_PATH)
        if home_path_length:
            candidates.append((home_path_length, 7, "path-start"))

        lookup_length = self.match_length(self.LOOKUP_PATH)
        if lookup_length:
            candidates.append((lookup_length, 8, "lookup-path"))

        uri_length = self.match_length(self.URI)
        if uri_length:
            candidates.append((uri_length, 9, "uri"))

        if candidates:
            length, _priority, kind = max(
                candidates, key=lambda candidate: (candidate[0], -candidate[1])
            )
            result = self.consume_token(kind, length)
            if kind == "path-start":
                self.states.append(
                    "path-slash" if result.lexeme.endswith(b"/") else "path"
                )
            elif result.lexeme == b"${":
                self.states.append("default")
            return result

        byte = self.data[self.offset]
        result = self.consume_token(None, 1)
        if byte == ord("{"):
            self.states.append("default")
        elif byte == ord("}") and len(self.states) > 1:
            self.states.pop()
        return result

    def double_string_token(self) -> NixToken | None:
        if self.offset >= len(self.data):
            return None
        if self.data[self.offset] == 0:
            raise self.error("NUL byte in double-quoted string")
        if self.data.startswith(b"${", self.offset):
            result = self.consume_token(None, 2)
            self.states.append("default")
            return result
        if self.data[self.offset] == 0x22:
            result = self.consume_token("double-quote", 1)
            self.states.pop()
            return result

        start = self.offset
        while self.offset < len(self.data):
            if self.data[self.offset] == 0:
                raise self.error("NUL byte in double-quoted string")
            if self.data.startswith(b"${", self.offset):
                break
            byte = self.data[self.offset]
            if byte == 0x22:
                break
            if byte == 0x5C:
                if self.offset + 1 >= len(self.data):
                    # The generated scanner reports EOF here.  Leaving the
                    # byte for a later call makes the grammar reject the
                    # unclosed string after any preceding fragment.
                    break
                # unescapeStr checks the backslash byte for NUL, but not the
                # byte consumed by the escape.  Thus an escaped raw NUL is an
                # observable accepted edge case of the implementation.
                self.offset += 2
            elif byte == ord("$"):
                if self.offset + 1 >= len(self.data):
                    break
                follower = self.data[self.offset + 1]
                if follower == ord("{"):
                    break
                if follower == ord('"'):
                    # Flex's trailing-context rule includes the final dollar
                    # in the fragment and then re-scans the quote.
                    self.offset += 1
                elif follower == ord("\\"):
                    if self.offset + 2 >= len(self.data):
                        break
                    # As with a plain backslash escape, an escaped NUL in this
                    # three-byte atom escapes the implementation's NUL check.
                    self.offset += 3
                else:
                    if follower == 0:
                        raise self.error("NUL byte in double-quoted string")
                    # Consuming the follower is significant for `$${`: the
                    # second dollar cannot begin interpolation.
                    self.offset += 2
            else:
                self.offset += 1
        if self.offset == start:
            return None
        return self.token("string-fragment", start, self.offset)

    def indented_string_token(self) -> NixToken | None:
        if self.offset >= len(self.data):
            return None
        if self.data[self.offset] == 0:
            raise self.error("NUL byte in indented string")
        if self.data.startswith(b"${", self.offset):
            result = self.consume_token(None, 2)
            self.states.append("default")
            return result

        if self.data.startswith(b"''", self.offset):
            if self.data.startswith(b"'''", self.offset):
                pass
            elif self.data.startswith(b"''$", self.offset):
                pass
            elif self.data.startswith(b"''\\", self.offset):
                if self.offset + 3 >= len(self.data):
                    return None
            else:
                result = self.consume_token("indented-string-close", 2)
                self.states.pop()
                return result

        start = self.offset
        while self.offset < len(self.data):
            if self.data[self.offset] == 0:
                raise self.error("NUL byte in indented string")
            if self.data.startswith(b"${", self.offset):
                break
            if self.data.startswith(b"'''", self.offset):
                self.offset += 3
                continue
            if self.data.startswith(b"''$", self.offset):
                self.offset += 3
                continue
            if self.data.startswith(b"''\\", self.offset):
                if self.offset + 3 >= len(self.data):
                    self.offset = len(self.data)
                    break
                self.offset += 4
                continue
            if self.data.startswith(b"''", self.offset):
                break
            byte = self.data[self.offset]
            if byte == ord("$"):
                if (
                    self.offset + 1 < len(self.data)
                    and self.data[self.offset + 1] not in b"{'"
                ):
                    if self.data[self.offset + 1] == 0:
                        raise self.error("NUL byte in indented string")
                    self.offset += 2
                else:
                    self.offset += 1
            elif byte == ord("'"):
                if (
                    self.offset + 1 < len(self.data)
                    and self.data[self.offset + 1] not in b"'$"
                ):
                    if self.data[self.offset + 1] == 0:
                        raise self.error("NUL byte in indented string")
                    self.offset += 2
                else:
                    self.offset += 1
            else:
                self.offset += 1
        if self.offset == start:
            return None
        return self.token("indented-string-fragment", start, self.offset)

    def path_token(self) -> NixToken:
        state = self.states[-1]
        if self.offset >= len(self.data):
            if state == "path-slash":
                raise self.error("path has a trailing slash")
            self.states.pop()
            return self.token(
                "path-end", self.offset, self.offset, virtual=True
            )

        if self.data.startswith(b"${", self.offset):
            result = self.consume_token(None, 2)
            self.states[-1] = "path"
            self.states.append("default")
            return result

        candidates = (
            self.match_length(self.PATH),
            self.match_length(self.PATH_SEGMENT),
            self.match_length(self.PATH_CHARACTERS),
        )
        length = max(candidates)
        if length:
            result = self.consume_token("path-fragment", length)
            self.states[-1] = (
                "path-slash" if result.lexeme.endswith(b"/") else "path"
            )
            return result

        if state == "path-slash":
            raise self.error("path has a trailing slash")
        self.states.pop()
        return self.token("path-end", self.offset, self.offset, virtual=True)

    def tokens(self) -> list[NixToken]:
        result: list[NixToken] = []
        while self.offset < len(self.data) or len(self.states) > 1:
            state = self.states[-1]
            if state == "default":
                token = self.default_token()
            elif state == "double-string":
                token = self.double_string_token()
            elif state == "indented-string":
                token = self.indented_string_token()
            elif state in {"path", "path-slash"}:
                token = self.path_token()
            else:
                raise AssertionError(state)

            if token is None:
                break
            result.append(token)
        return result


@dataclasses.dataclass(frozen=True)
class EarleyState:
    left: str
    right: tuple[GrammarSymbol, ...]
    dot: int
    origin: int

    def next_symbol(self) -> GrammarSymbol | None:
        if self.dot == len(self.right):
            return None
        return self.right[self.dot]

    def advance(self) -> EarleyState:
        return EarleyState(self.left, self.right, self.dot + 1, self.origin)


@dataclasses.dataclass(frozen=True)
class RecognitionFailure:
    position: int
    expected: tuple[GrammarTerminal, ...]


def terminal_matches(terminal: GrammarTerminal, token: NixToken) -> bool:
    if terminal.category == "token":
        return token.kind == terminal.value
    try:
        encoded = terminal.value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return token.kind is None and token.lexeme == encoded


def recognize(
    grammar: BnfGrammar, tokens: Sequence[NixToken]
) -> RecognitionFailure | None:
    augmented = "@start"
    start_state = EarleyState(augmented, (grammar.start,), 0, 0)
    chart: list[set[EarleyState]] = [set() for _ in range(len(tokens) + 1)]
    agendas: list[collections.deque[EarleyState]] = [
        collections.deque() for _ in range(len(tokens) + 1)
    ]
    waiting: list[dict[str, set[EarleyState]]] = [
        collections.defaultdict(set) for _ in range(len(tokens) + 1)
    ]
    nullable_complete: list[dict[str, set[EarleyState]]] = [
        collections.defaultdict(set) for _ in range(len(tokens) + 1)
    ]

    def add(position: int, state: EarleyState) -> None:
        if state not in chart[position]:
            chart[position].add(state)
            agendas[position].append(state)

    add(0, start_state)
    farthest = 0

    for position in range(len(tokens) + 1):
        agenda = agendas[position]
        while agenda:
            state = agenda.popleft()
            symbol = state.next_symbol()
            if symbol is None:
                if state.origin == position:
                    nullable_complete[position][state.left].add(state)
                for waiter in tuple(waiting[state.origin].get(state.left, ())):
                    add(position, waiter.advance())
            elif isinstance(symbol, str):
                waiting[position][symbol].add(state)
                for right in grammar.rules.get(symbol, ()):
                    add(position, EarleyState(symbol, right, 0, position))
                if nullable_complete[position].get(symbol):
                    add(position, state.advance())

        if chart[position]:
            farthest = position
        if position == len(tokens):
            continue
        token = tokens[position]
        for state in chart[position]:
            symbol = state.next_symbol()
            if isinstance(symbol, GrammarTerminal) and terminal_matches(symbol, token):
                add(position + 1, state.advance())

    accepted = EarleyState(augmented, (grammar.start,), 1, 0)
    if accepted in chart[len(tokens)]:
        return None

    expected = {
        symbol
        for state in chart[farthest]
        if isinstance((symbol := state.next_symbol()), GrammarTerminal)
    }
    return RecognitionFailure(
        farthest,
        tuple(sorted(expected, key=lambda terminal: terminal.describe())),
    )


def format_recognition_failure(
    source: str,
    data: bytes,
    tokens: Sequence[NixToken],
    failure: RecognitionFailure,
) -> str:
    if failure.position < len(tokens):
        actual = tokens[failure.position]
        location = f"{source}:{actual.line}:{actual.column}"
        found = actual.describe()
    else:
        lexer = NixLexer(data, source)
        line, column = lexer.position(len(data))
        location = f"{source}:{line}:{column}"
        found = "end of input"
    expected = ", ".join(item.describe() for item in failure.expected[:12])
    if len(failure.expected) > 12:
        expected += ", ..."
    if not expected:
        expected = "no viable token"
    return f"{location}: found {found}; expected {expected}"


def recognize_source(
    grammar: BnfGrammar, data: bytes, source: str
) -> tuple[bool, str | None]:
    try:
        tokens = NixLexer(data, source).tokens()
    except NixLexError as error:
        return False, str(error)
    failure = recognize(grammar, tokens)
    if failure is None:
        return True, None
    return False, format_recognition_failure(source, data, tokens, failure)


STATIC_GRAMMAR_POSITIVES = {
    "parse-fail-dup-attrs-1.nix",
    "parse-fail-dup-attrs-2.nix",
    "parse-fail-dup-attrs-3.nix",
    "parse-fail-dup-attrs-4.nix",
    "parse-fail-dup-attrs-7.nix",
    "parse-fail-dup-formals.nix",
    "parse-fail-mixed-nested-attrs1.nix",
    "parse-fail-mixed-nested-attrs2.nix",
    "parse-fail-patterns-1.nix",
    "parse-fail-regression-20060610.nix",
    "parse-fail-undef-var.nix",
}

DYNAMIC_CONTEXT_POSITIVES = {
    "eval-fail-dynamic-attrs-inherit-2.nix",
    "eval-fail-dynamic-attrs-inherit.nix",
    "eval-fail-dynamic-attrs-let-2.nix",
    "eval-fail-dynamic-attrs-let-3.nix",
    "eval-fail-dynamic-attrs-let.nix",
}

SUPPORT_POSITIVES = {
    "imported.nix",
    "imported2.nix",
    "infinite-nesting.nix",
    "lib.nix",
    "non-eval-fail-bad-drvPath.nix",
    "non-eval-trivial-lambda-formals.nix",
}

GRAMMAR_NEGATIVES = {
    "parse-fail-eof-in-string.nix",
    "parse-fail-eof-pos.nix",
    "parse-fail-undef-var-2.nix",
    "parse-fail-unexpected-and.nix",
    "parse-fail-unexpected-assert.nix",
    "parse-fail-unexpected-concat.nix",
    "parse-fail-unexpected-ellipsis.nix",
    "parse-fail-unexpected-else.nix",
    "parse-fail-unexpected-eq.nix",
    "parse-fail-unexpected-float.nix",
    "parse-fail-unexpected-geq.nix",
    "parse-fail-unexpected-hpath.nix",
    "parse-fail-unexpected-if.nix",
    "parse-fail-unexpected-impl.nix",
    "parse-fail-unexpected-in.nix",
    "parse-fail-unexpected-ind-string.nix",
    "parse-fail-unexpected-inherit.nix",
    "parse-fail-unexpected-int.nix",
    "parse-fail-unexpected-leq.nix",
    "parse-fail-unexpected-let.nix",
    "parse-fail-unexpected-neq.nix",
    "parse-fail-unexpected-or.nix",
    "parse-fail-unexpected-path.nix",
    "parse-fail-unexpected-rec.nix",
    "parse-fail-unexpected-spath.nix",
    "parse-fail-unexpected-then.nix",
    "parse-fail-unexpected-update.nix",
    "parse-fail-unexpected-uri.nix",
    "parse-fail-unexpected-with.nix",
    "parse-fail-utf8.nix",
}

INLINE_POSITIVES = {
    "identifier-boundary": b"a-b",
    "maximum-integer": b"9223372036854775807",
    "lambda": b"x: x",
    "conditional": b"if true then 1 else 2",
    "application-and-selection": b"(f x).name or fallback",
    "double-string-interpolation": b'"left ${1 + 2} right"',
    "indented-string-interpolation": b"''left ${1 + 2} right''",
    "path-interpolation": b"./left/${x}/right",
    "repeated-path-slash-edge": b"/foo//bar${x}",
    "uri-priority": b"x:x",
    "path-priority": b"1/2",
    "division-spacing": b"1 / 2",
    "right-pipe": b"1 |> f |> g",
    "left-pipe": b"f <| g <| 1",
    "escaped-nul-double-string": b'"a\\\0b"',
    "escaped-nul-indented-string": b"''''\\\0''",
    "nul-in-line-comment": b"1 # comment\0suffix\n + 2",
    "nul-in-block-comment": b"1 /* comment\0suffix */ + 2",
    "nul-in-documentation-comment": b"1 /** comment\0suffix */ + 2",
    "double-dollar-before-interpolation-marker": b'"$${"',
    "indented-double-dollar-before-interpolation-marker": b"''$${''",
}

RAW_INLINE_POSITIVES = {
    # In the scanner's normal state, the fallback token for NUL has numeric
    # value zero and is therefore interpreted by Bison as EOF.  This must be
    # checked without the free-variable-closing wrapper, which the NUL would
    # hide from the source parser.
    "normal-state-nul-is-eof": b"1\0 ignored",
    "path-state-nul-is-eof": b"./foo\0 ignored",
}

REPL_INLINE_POSITIVES = {
    "repl-one-binding": b"a = 1;",
    "repl-multiple-bindings": b"a = 1; inherit b;",
}

REPL_INLINE_NEGATIVES = {
    "repl-empty": b"",
    "repl-expression": b"1",
    "repl-missing-semicolon": b"a = 1",
}

LEXICAL_LIMIT_NEGATIVES = {
    "integer-overflow": b"9223372036854775808",
    "floating-point-overflow": b"1.0e9999",
    "floating-point-underflow": b"1.0e-9999",
}

INLINE_NEGATIVES = {
    "empty-file": b"",
    "missing-conditional-else": b"if true then 1",
    "missing-binding-semicolon": b"{ a = 1 }",
    "comparison-chain": b"1 < 2 < 3",
    "equality-chain": b"1 == 2 == 3",
    "mixed-pipe-directions": b"1 |> f <| g",
    "trailing-path-slash": b"./foo/",
    "zero-dot-is-not-float": b"0.",
    "three-slash-path-edge": b"/foo///bar${x}",
    "nul-after-path-slash": b"./foo/\0 ignored",
    "ordinary-nul-double-string": b'"a\0b"',
    "ordinary-nul-indented-string": b"''a\0b''",
}


def corpus_paths(
    corpus: Path,
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    if not corpus.is_dir():
        raise ValidationError(f"{corpus}: syntax corpus directory does not exist")

    parse_fail_names = {
        path.name for path in corpus.glob("parse-fail-*.nix")
    }
    classified_failures = STATIC_GRAMMAR_POSITIVES | GRAMMAR_NEGATIVES
    unclassified = parse_fail_names - classified_failures
    missing = classified_failures - parse_fail_names
    if unclassified or missing:
        details: list[str] = []
        if unclassified:
            details.append(
                "unclassified parse-fail files: " + ", ".join(sorted(unclassified))
            )
        if missing:
            details.append(
                "classified files no longer present: " + ", ".join(sorted(missing))
            )
        raise ValidationError(f"{corpus}: " + "; ".join(details))

    base_positive = (
        sorted(corpus.glob("parse-okay-*.nix"))
        + sorted(corpus.glob("eval-okay-*.nix"))
        + [corpus / name for name in sorted(SUPPORT_POSITIVES)]
    )
    static_positive = [
        corpus / name for name in sorted(STATIC_GRAMMAR_POSITIVES)
    ]
    dynamic_context_positive = [
        corpus / name for name in sorted(DYNAMIC_CONTEXT_POSITIVES)
    ]
    negative = [corpus / name for name in sorted(GRAMMAR_NEGATIVES)]

    missing_support = [
        path
        for path in [*base_positive, *dynamic_context_positive]
        if not path.is_file()
    ]
    if missing_support:
        raise ValidationError(
            "missing positive corpus file(s): "
            + ", ".join(str(path) for path in missing_support)
        )
    return base_positive, static_positive, dynamic_context_positive, negative


def check_expected(
    grammar: BnfGrammar,
    positives: Sequence[tuple[str, bytes]],
    negatives: Sequence[tuple[str, bytes]],
) -> None:
    failures: list[str] = []
    for source, data in positives:
        accepted, detail = recognize_source(grammar, data, source)
        if not accepted:
            failures.append(f"expected grammar acceptance: {detail}")
    for source, data in negatives:
        accepted, _detail = recognize_source(grammar, data, source)
        if accepted:
            failures.append(f"{source}: grammar unexpectedly accepted input")
    if failures:
        displayed = failures[:30]
        if len(failures) > len(displayed):
            displayed.append(f"... and {len(failures) - len(displayed)} more failure(s)")
        raise ValidationError("\n".join(displayed))


def oracle_environment() -> dict[str, str]:
    environment = os.environ.copy()
    extra_config = """
extra-experimental-features = pipe-operators
lint-url-literals = ignore
lint-short-path-literals = ignore
lint-absolute-path-literals = ignore
"""
    environment["NIX_CONFIG"] = environment.get("NIX_CONFIG", "") + extra_config
    return environment


def source_parser_accepts(
    executable: Path,
    data: bytes,
    *,
    close_free_variables: bool,
    unified_nix: bool,
) -> tuple[bool, str]:
    if close_free_variables:
        data = b"with {}; (\n" + data + b"\n)"
    if unified_nix:
        arguments = ["nix-instantiate", "--parse", "-"]
        executable_override: str | None = str(executable)
    else:
        arguments = [str(executable), "--parse", "-"]
        executable_override = None
    try:
        process = subprocess.run(
            arguments,
            executable=executable_override,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=oracle_environment(),
            timeout=30,
            check=False,
        )
    except OSError as error:
        raise ValidationError(f"cannot execute source parser {executable}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"source parser timed out on an input: {error}") from error
    diagnostic = process.stderr.decode("utf-8", "backslashreplace").strip()
    return process.returncode == 0, diagnostic


def check_source_parser(
    executable: Path,
    base_positive: Sequence[tuple[str, bytes]],
    static_positive: Sequence[tuple[str, bytes]],
    dynamic_context_positive: Sequence[tuple[str, bytes]],
    grammar_negative: Sequence[tuple[str, bytes]],
    *,
    unified_nix: bool,
) -> int:
    if not executable.is_file():
        raise ValidationError(f"{executable}: source parser is not a file")
    if not os.access(executable, os.X_OK):
        raise ValidationError(f"{executable}: source parser is not executable")

    failures: list[str] = []
    checked = 0

    for source, data in [*base_positive, *INLINE_POSITIVES.items()]:
        # Exercise the repository's explicit parser-positive corpus byte for
        # byte.  Other positive syntax fixtures are wrapped in a `with` so the
        # source parser's subsequent undefined-variable pass does not create
        # false grammar mismatches.
        is_parse_okay = Path(source).name.startswith("parse-okay-")
        accepted, diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=not is_parse_okay,
            unified_nix=unified_nix,
        )
        checked += 1
        if not accepted:
            failures.append(
                f"{source}: source parser rejected grammar-positive input"
                + (f": {diagnostic}" if diagnostic else "")
            )

    for source, data in RAW_INLINE_POSITIVES.items():
        accepted, diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=False,
            unified_nix=unified_nix,
        )
        checked += 1
        if not accepted:
            failures.append(
                f"{source}: source parser rejected raw grammar-positive input"
                + (f": {diagnostic}" if diagnostic else "")
            )

    for source, data in grammar_negative:
        accepted, _diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=True,
            unified_nix=unified_nix,
        )
        checked += 1
        if accepted:
            failures.append(
                f"{source}: source parser accepted grammar-negative input"
            )

    for source, data in INLINE_NEGATIVES.items():
        accepted, _diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=True,
            unified_nix=unified_nix,
        )
        checked += 1
        if accepted:
            failures.append(
                f"{source}: source parser accepted grammar-negative input"
            )

    # Numeric spelling is context-free, but lexer conversion imposes the
    # implementation ranges documented next to the lexical grammar.
    for source, data in LEXICAL_LIMIT_NEGATIVES.items():
        accepted, _diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=False,
            unified_nix=unified_nix,
        )
        checked += 1
        if accepted:
            failures.append(
                f"{source}: source parser accepted a documented lexical "
                "range violation"
            )

    # These are deliberately accepted by the context-free grammar but rejected
    # by parser actions or the subsequent static binding pass.
    for source, data in static_positive:
        accepted, _diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=False,
            unified_nix=unified_nix,
        )
        checked += 1
        if accepted:
            failures.append(
                f"{source}: source parser no longer rejects the documented "
                "contextual/static restriction"
            )

    # These fixtures exercise parser-action restrictions, but some end in a
    # free variable. Close their free variables so an undefined-name error
    # cannot mask removal of the intended restriction.
    for source, data in dynamic_context_positive:
        accepted, _diagnostic = source_parser_accepts(
            executable,
            data,
            close_free_variables=True,
            unified_nix=unified_nix,
        )
        checked += 1
        if accepted:
            failures.append(
                f"{source}: source parser no longer rejects the documented "
                "dynamic-attribute restriction"
            )

    if failures:
        displayed = failures[:30]
        if len(failures) > len(displayed):
            displayed.append(f"... and {len(failures) - len(displayed)} more failure(s)")
        raise ValidationError("\n".join(displayed))
    return checked


def load_files(paths: Sequence[Path]) -> list[tuple[str, bytes]]:
    result = []
    for path in paths:
        try:
            result.append((str(path), path.read_bytes()))
        except OSError as error:
            raise ValidationError(f"{path}: cannot read corpus input: {error}") from error
    return result


def select_nixpkgs_sample(
    root: Path,
    sample_size: int,
    seed: int,
) -> list[Path]:
    """Select production package expressions while avoiding syntax fixtures."""

    package_root = root / "pkgs"
    if not package_root.is_dir():
        raise ValidationError(f"{root}: expected a Nixpkgs checkout containing pkgs/")

    excluded_parts = {
        "fixture",
        "fixtures",
        "test",
        "test-data",
        "test_data",
        "testdata",
        "tests",
    }
    candidates: list[Path] = []
    for path in package_root.rglob("*.nix"):
        relative = path.relative_to(package_root)
        if any(part.lower() in excluded_parts for part in relative.parts):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 512 * 1024:
                continue
        except OSError as error:
            raise ValidationError(f"{path}: cannot inspect Nixpkgs input: {error}") from error
        candidates.append(path)

    if len(candidates) < sample_size:
        raise ValidationError(
            f"{root}: requested {sample_size} Nixpkgs files, but only "
            f"{len(candidates)} eligible files were found"
        )

    seed_bytes = str(seed).encode("ascii") + b"\0"

    def sample_key(path: Path) -> tuple[bytes, str]:
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(seed_bytes + relative.encode("utf-8")).digest()
        return digest, relative

    candidates.sort(key=sample_key)
    return candidates[:sample_size]


def check_nixpkgs_sample(
    grammar: BnfGrammar,
    executable: Path,
    root: Path,
    *,
    sample_size: int,
    seed: int,
    unified_nix: bool,
) -> int:
    """Differentially recognize a deterministic random sample from Nixpkgs."""

    paths = select_nixpkgs_sample(root, sample_size, seed)

    def check_one(path: Path) -> tuple[Path, bool, str | None, bool, str]:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ValidationError(f"{path}: cannot read Nixpkgs input: {error}") from error
        grammar_accepted, grammar_detail = recognize_source(grammar, data, str(path))
        parser_accepted, parser_detail = source_parser_accepts(
            executable,
            data,
            close_free_variables=True,
            unified_nix=unified_nix,
        )
        return (
            path,
            grammar_accepted,
            grammar_detail,
            parser_accepted,
            parser_detail,
        )

    workers = min(8, max(1, os.cpu_count() or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(check_one, paths))

    failures: list[str] = []
    for path, grammar_accepted, grammar_detail, parser_accepted, parser_detail in results:
        if not grammar_accepted:
            failures.append(
                f"{path}: documented grammar rejected Nixpkgs input: "
                f"{grammar_detail}"
            )
        if not parser_accepted:
            failures.append(
                f"{path}: source parser rejected sampled Nixpkgs input"
                + (f": {parser_detail}" if parser_detail else "")
            )

    if failures:
        displayed = failures[:30]
        if len(failures) > len(displayed):
            displayed.append(f"... and {len(failures) - len(displayed)} more failure(s)")
        raise ValidationError("\n".join(displayed))
    return len(results)


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer, found {value!r}") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return result


def integer_with_base(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"expected an integer seed, found {value!r}"
        ) from error


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grammar",
        type=Path,
        default=DEFAULT_GRAMMAR,
        help=f"grammar Markdown file (default: {DEFAULT_GRAMMAR})",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"functional language corpus (default: {DEFAULT_CORPUS})",
    )
    oracle = parser.add_mutually_exclusive_group()
    oracle.add_argument(
        "--nix-instantiate",
        type=Path,
        help="checkout-built nix-instantiate used as a differential oracle",
    )
    oracle.add_argument(
        "--nix",
        type=Path,
        help=(
            "checkout-built unified nix executable; it is invoked with "
            "argv[0] set to nix-instantiate"
        ),
    )
    parser.add_argument(
        "--skip-corpus",
        action="store_true",
        help="only parse and audit the Markdown EBNF plus inline probes",
    )
    parser.add_argument(
        "--nixpkgs",
        type=Path,
        help=(
            "optionally stress-test a deterministic sample of production "
            "expressions from this Nixpkgs checkout"
        ),
    )
    parser.add_argument(
        "--nixpkgs-sample-size",
        type=positive_integer,
        default=2000,
        help="number of Nixpkgs files to sample (default: 2000)",
    )
    parser.add_argument(
        "--nixpkgs-seed",
        type=integer_with_base,
        default=0x4E4958,
        help="deterministic Nixpkgs sample seed (default: 0x4e4958)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    arguments = parse_arguments(argv)
    grammar_path = arguments.grammar.resolve()

    lexical = parse_ebnf(grammar_path, "lexical-grammar")
    syntax = parse_ebnf(grammar_path, "syntactic-grammar")

    overlap = lexical.keys() & syntax.keys()
    if overlap:
        raise ValidationError(
            "productions occur in both grammar blocks: " + ", ".join(sorted(overlap))
        )

    audit_graph("lexical", lexical, ["lexer-item"])
    lexical_contract = ebnf_contract_digest(lexical)
    if lexical_contract != NIX_LEXER_TRANSCRIPTION_CONTRACT_SHA256:
        raise ValidationError(
            "lexical EBNF changed without revalidating the handwritten "
            "scanner transcription; expected contract "
            f"{NIX_LEXER_TRANSCRIPTION_CONTRACT_SHA256}, found "
            f"{lexical_contract}"
        )

    lexical_tokens = direct_reference_alternatives(lexical["lexical-token"])
    if lexical_tokens != EXPECTED_LEXICAL_TOKENS:
        raise ValidationError(
            "direct lexical-token alternatives do not match scanner support; "
            f"missing={sorted(EXPECTED_LEXICAL_TOKENS - lexical_tokens)}, "
            f"unexpected={sorted(lexical_tokens - EXPECTED_LEXICAL_TOKENS)}"
        )

    fixed_tokens = finite_literal_language(lexical, "fixed-token")
    syntax_literals = {
        node.value
        for production in syntax.values()
        for node in walk(production.expression)
        if isinstance(node, Literal)
    }
    if fixed_tokens != syntax_literals:
        raise ValidationError(
            "fixed-token does not match the syntactic literal inventory; "
            f"missing={sorted(syntax_literals - fixed_tokens)}, "
            f"unexpected={sorted(fixed_tokens - syntax_literals)}"
        )

    scanner_fixed_tokens = (
        {token.decode("ascii") for token in NixLexer.KEYWORDS}
        | {token.decode("ascii") for token in NixLexer.OPERATORS}
        | set(NixLexer.SINGLE_CHARACTER_TOKENS)
    )
    if fixed_tokens != scanner_fixed_tokens:
        raise ValidationError(
            "fixed-token does not match the validation scanner inventory; "
            f"missing={sorted(scanner_fixed_tokens - fixed_tokens)}, "
            f"unexpected={sorted(fixed_tokens - scanner_fixed_tokens)}"
        )

    audit_graph(
        "syntactic",
        syntax,
        ["nix-file", "repl-bindings"],
        allowed_external=lexical_tokens,
    )
    bnf = BnfGrammar(syntax, lexical_tokens, "nix-file")
    repl_bnf = BnfGrammar(syntax, lexical_tokens, "repl-bindings")

    inline_positive = [
        *INLINE_POSITIVES.items(),
        *RAW_INLINE_POSITIVES.items(),
    ]
    inline_negative = list(INLINE_NEGATIVES.items())
    check_expected(bnf, inline_positive, inline_negative)
    check_expected(
        repl_bnf,
        list(REPL_INLINE_POSITIVES.items()),
        list(REPL_INLINE_NEGATIVES.items()),
    )

    base_positive: list[tuple[str, bytes]] = []
    static_positive: list[tuple[str, bytes]] = []
    dynamic_context_positive: list[tuple[str, bytes]] = []
    grammar_negative: list[tuple[str, bytes]] = []
    if not arguments.skip_corpus:
        (
            base_paths,
            static_paths,
            dynamic_context_paths,
            negative_paths,
        ) = corpus_paths(arguments.corpus.resolve())
        base_positive = load_files(base_paths)
        static_positive = load_files(static_paths)
        dynamic_context_positive = load_files(dynamic_context_paths)
        grammar_negative = load_files(negative_paths)
        check_expected(
            bnf,
            [
                *base_positive,
                *static_positive,
                *dynamic_context_positive,
            ],
            grammar_negative,
        )

    oracle_count = 0
    oracle_path = arguments.nix or arguments.nix_instantiate
    if oracle_path is not None:
        # nix-instantiate is normally a symlink to the multi-call `nix`
        # executable.  Preserve argv[0]; resolving the symlink would select
        # the wrong command personality.
        oracle_count = check_source_parser(
            oracle_path.absolute(),
            base_positive,
            static_positive,
            dynamic_context_positive,
            grammar_negative,
            unified_nix=arguments.nix is not None,
        )

    nixpkgs_count = 0
    if arguments.nixpkgs is not None:
        if oracle_path is None:
            raise ValidationError(
                "--nixpkgs requires either --nix or --nix-instantiate"
            )
        nixpkgs_count = check_nixpkgs_sample(
            bnf,
            oracle_path.absolute(),
            arguments.nixpkgs.resolve(),
            sample_size=arguments.nixpkgs_sample_size,
            seed=arguments.nixpkgs_seed,
            unified_nix=arguments.nix is not None,
        )

    positive_count = (
        len(inline_positive)
        + len(REPL_INLINE_POSITIVES)
        + len(base_positive)
        + len(static_positive)
        + len(dynamic_context_positive)
    )
    negative_count = (
        len(inline_negative)
        + len(REPL_INLINE_NEGATIVES)
        + len(grammar_negative)
    )
    message = (
        f"validated {len(lexical)} lexical and {len(syntax)} syntactic "
        f"productions; {positive_count} positive and {negative_count} negative "
        "syntax inputs"
    )
    if oracle_count:
        message += f"; {oracle_count} source-parser comparisons"
    if nixpkgs_count:
        message += (
            f"; {nixpkgs_count} Nixpkgs files "
            f"(seed {arguments.nixpkgs_seed:#x})"
        )
    print(message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ValidationError as error:
        print(f"language grammar validation failed:\n{error}", file=sys.stderr)
        raise SystemExit(1)

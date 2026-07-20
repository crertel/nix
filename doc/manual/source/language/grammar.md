# Nix language grammar

This page gives the complete concrete grammar accepted by the Nix parser.
It is a reference for language implementers and tooling authors; the other
[language reference pages](./index.md) explain the constructs and their
semantics.

The grammar has two layers because string interpolation and path literals make
the lexer stateful:

- the lexical grammar describes tokens and scanner states;
- the syntactic grammar describes the token sequences accepted by the parser.

The grammar describes the default language before configuration-dependent
diagnostics are applied. In particular, URI and path-literal diagnostics may
be configured to reject otherwise valid syntax, and the pipe operators require
the [`pipe-operators` experimental feature](./operators.md#pipe-operators).

## Notation

The grammar uses ISO-style EBNF with these conventions:

- `=` defines a production and `;` ends it;
- `,` joins items in sequence;
- `|` separates alternatives;
- `[ ... ]` is an optional item;
- `{ ... }` is a zero-or-more repetition;
- `( ... )` groups items;
- quoted text is an exact sequence of source bytes;
- `? ... ?` is a prose-defined character predicate or scanner condition;
- `(* ... *)` is a grammar comment.

Except inside strings and paths, any amount of [trivia](#trivia) may occur
between tokens. Flex's longest-match rule applies: the scanner chooses the
longest token available and, for equal-length matches, the rule listed first
in the implementation. This is observable syntax. For example:

- `a-b` is one identifier, while `a - b` is subtraction;
- `1/2` is a path, while `1 / 2` is division;
- `x:x` is a URI, while `x: x` is a function;
- `1.` is a floating-point literal, while `0.` is not.

## Lexical grammar

The direct alternatives of `lexical-token` are the non-fixed or
scanner-state-dependent lexical categories used by the syntactic grammar.
Productions below those alternatives describe their source spelling.
`fixed-token` lists the keywords, operators, and punctuation that are quoted
directly in the syntactic grammar.

<!-- lexical-grammar -->

```ebnf
lexer-item = lexical-token | fixed-token | trivia ;

lexical-token
  = identifier
  | integer
  | floating-point
  | double-quote
  | string-fragment
  | indented-string-open
  | indented-string-fragment
  | indented-string-close
  | path-start
  | path-fragment
  | path-end
  | lookup-path
  | uri
  ;

fixed-token
  = keyword
  | multi-character-token
  | single-character-token
  ;
keyword
  = "if" | "then" | "else" | "assert" | "with" | "let" | "in"
  | "rec" | "inherit" | "or"
  ;
multi-character-token
  = "..." | "==" | "!=" | "<=" | ">=" | "&&" | "||" | "->"
  | "//" | "++" | "<|" | "|>" | "${"
  ;
single-character-token
  = ":" | "@" | ";" | "{" | "}" | "(" | ")" | "." | ","
  | "=" | "!" | "-" | "<" | ">" | "+" | "*" | "/" | "?"
  | "[" | "]"
  ;

ascii-letter = ? one ASCII byte in A-Z or a-z ? ;
decimal-digit = ? one ASCII byte in 0-9 ? ;
nonzero-digit = ? one ASCII byte in 1-9 ? ;
backslash = ? one U+005C REVERSE SOLIDUS byte ? ;

identifier-start = ascii-letter | "_" ;
identifier-continue
  = ascii-letter
  | decimal-digit
  | "_"
  | "'"
  | "-"
  ;
identifier = identifier-start, { identifier-continue } ;

integer = decimal-digit, { decimal-digit } ;
exponent
  = ("E" | "e"), [ "+" | "-" ], decimal-digit, { decimal-digit }
  ;
floating-point
  = (
      nonzero-digit, { decimal-digit }, ".", { decimal-digit }
    | [ "0" ], ".", decimal-digit, { decimal-digit }
    ),
    [ exponent ]
  ;

path-character
  = ascii-letter
  | decimal-digit
  | "."
  | "_"
  | "-"
  | "+"
  ;
nonempty-path-component = path-character, { path-character } ;
path-segment = { path-character }, "/" ;
path-token
  = { path-character },
    "/", nonempty-path-component,
    { "/", nonempty-path-component },
    [ "/" ]
  ;
home-path-token
  = "~",
    "/", nonempty-path-component,
    { "/", nonempty-path-component },
    [ "/" ]
  ;
path-start
  = path-token
  | home-path-token
  | path-segment, ? immediately followed by `${` ?
  | "~/", ? immediately followed by `${` ?
  ;
path-continuation
  = path-token
  | path-segment
  | path-character, { path-character }
  ;
path-end
  = ? zero width while in the non-slash path state, before a byte
      that cannot continue the path, or at EOF ?
  ;

lookup-path
  = "<",
    nonempty-path-component,
    { "/", nonempty-path-component },
    ">"
  ;

uri-scheme
  = ascii-letter,
    { ascii-letter | decimal-digit | "+" | "-" | "." }
  ;
uri-body-character
  = ascii-letter
  | decimal-digit
  | "%"
  | "/"
  | "?"
  | ":"
  | "@"
  | "&"
  | "="
  | "+"
  | "$"
  | ","
  | "-"
  | "_"
  | "."
  | "!"
  | "~"
  | "*"
  | "'"
  ;
uri = uri-scheme, ":", uri-body-character, { uri-body-character } ;

double-quote = ? one U+0022 QUOTATION MARK byte ? ;
any-byte = ? any byte, including LF ? ;
double-string-ordinary
  = ? any byte except `$`, U+0022, and backslash ?
  ;
double-string-dollar-follower
  = ? any byte except `{`, U+0022, and backslash ?
  ;
double-string-atom
  = double-string-ordinary
  | "$", double-string-dollar-follower
  | backslash, any-byte
  | "$", backslash, any-byte
  ;
double-string-fragment
  = double-string-atom, { double-string-atom }
  | { double-string-atom },
    "$", ? immediately followed by double-quote ?
  ;

string-fragment
  = double-string-fragment, ? while scanning a double-quoted string ?
  ;
path-fragment = path-continuation, ? while scanning a path ? ;

line-feed = ? one LF byte ? ;
indented-string-open = "''", [ { " " }, line-feed ] ;
indented-string-close = "''" ;
indented-string-ordinary = ? any byte except `$` and apostrophe ? ;
indented-string-dollar-follower = ? any byte except `{` and apostrophe ? ;
indented-string-apostrophe-follower
  = ? any byte except apostrophe and `$` ?
  ;
indented-string-atom
  = indented-string-ordinary
  | "$", indented-string-dollar-follower
  | "'", indented-string-apostrophe-follower
  ;
indented-string-fragment
  = indented-string-atom, { indented-string-atom }
  | "''$"
  | "$"
  | "'''"
  | "''", backslash, any-byte
  | "'"
  ;

space = " " | ? one TAB, CR, or LF byte ? ;
whitespace = space, { space } ;
line-comment
  = "#", { ? any byte except CR and LF ? }
  ;
block-comment
  = "/*",
    ? zero or more bytes ending immediately before the first subsequent `*/` ?,
    "*/"
  ;
trivia = whitespace | line-comment | block-comment ;
```

`path-end` is a synthetic scanner token and consumes no source bytes.
End-of-input and the private token that selects REPL bindings mode are parser
control events, not `lexer-item` source tokens. The Bison-only `NOT` and
`NEGATE` names assign precedence to unary operators and are not scanner tokens.

### Keywords

The reserved keywords are:

```text
if then else assert with let in rec inherit or
```

`true`, `false`, and `null` are ordinary identifiers supplied by the initial
scope, not keywords. Exact keyword matches take priority over `identifier`;
longer matches such as `inheritance` remain identifiers. The `or` keyword has
two compatibility exceptions in the syntactic grammar: it is accepted as an
unquoted attribute name and in the legacy “cursed or” application form.

### Numeric range checks

An `integer` token must fit the signed 64-bit Nix integer type. A
`floating-point` token must be accepted by the host `strtod` conversion without
a range error. Leading `-` is never part of either token; it is an operator.

### String scanner states

After `double-quote`, the scanner emits `string-fragment` tokens until
it sees another `double-quote` or `${`. In a double-quoted string, backslash
consumes the next byte. `\n`, `\r`, and `\t` denote LF, CR, and TAB;
every other escape removes the backslash. Literal CR and CRLF are normalized
to LF. `$${` is literal text, while `${` starts an interpolation.

After `indented-string-open`, the scanner emits `indented-string-fragment`
tokens until `indented-string-close` or `${`. Longest match gives the escape
forms priority over the closing delimiter:

- `'''` denotes `''`;
- `''$` denotes `$`;
- `''\x` applies the double-quoted-string unescaping rule to `x`.

The optional spaces and LF matched by `indented-string-open` are discarded.
Indentation stripping is a value transformation and does not change which
source texts match the grammar.

### NUL handling

The byte-oriented scanner has two observable NUL edge cases. An ordinary NUL
in either kind of string is rejected, but a NUL consumed as the byte after a
backslash is not checked and is accepted. A NUL covered by a line, block, or
documentation comment is consumed as trivia.

A standalone NUL that reaches the fallback rule in the normal expression
state is returned as token number zero, which Bison interprets as end-of-input.
In the non-slash path state, the scanner first emits `path-end` and then
reprocesses the NUL with that fallback rule; in the slash path state it instead
reports a trailing-slash error. Consequently, a complete expression or
non-slash path followed by NUL and more bytes parses as though the NUL and
suffix were absent.

### Path scanner states

`path-start` must contain a slash before the first interpolation. It switches
the scanner into a path state. In that state, `${` starts an interpolation and
`path-fragment` contains static path text. `path-end` is a synthetic,
zero-width token emitted at the first byte that cannot continue the path or at
EOF.

A literal slash immediately before an interpolation is valid. A path may also
end immediately after an interpolation, but a final literal slash is a parse
error. Comments and whitespace are not trivia inside a path.

The token-level rules deliberately preserve an implementation edge case.
Before the first interpolation, the syntactic grammar accepts at most one
continuation token. Flex may split repeated slashes into multiple continuation
tokens, so `/foo//bar${x}` is accepted while `/foo///bar${x}` is rejected.
After the first interpolation, any number of continuation tokens is accepted.

### Trivia

Whitespace consists only of space, TAB, CR, and LF. A line comment ends before
CR or LF. A block comment ends at the next `*/` and cannot nest.
A documentation comment is a block comment whose `/**` prefix is followed by
a byte other than `/` or `*`; the scanner records its contents as metadata.
Consequently, `/**/` and `/***/` are ordinary block comments.

Trivia is recognized in the normal expression state and inside interpolation
expressions. It is not recognized inside the exclusive string or path states.
`${` and `{` push the normal expression state, and the matching `}` returns to
the enclosing string or path state. This stack permits nested braces and
interpolations.

## Syntactic grammar

`nix-file` is the start production for a source file or `--expr` input. The
entire token stream delivered by the scanner must be consumed after the
production matches. The [normal-state NUL behavior](#nul-handling) can end that
stream before the end of the source buffer.
`repl-bindings` is a separate start production used only after the REPL injects
its private bindings-mode token; it is not an alternative file syntax.

<!-- syntactic-grammar -->

```ebnf
nix-file = expression ;
repl-bindings = binding, { binding } ;

expression = function-expression ;

function-expression
  = identifier, ":", function-expression
  | formal-set, ":", function-expression
  | formal-set, "@", identifier, ":", function-expression
  | identifier, "@", formal-set, ":", function-expression
  | "assert", expression, ";", function-expression
  | "with", expression, ";", function-expression
  | "let", { binding }, "in", function-expression
  | conditional-expression
  ;

conditional-expression
  = "if", expression, "then", expression, "else", expression
  | pipe-from-expression
  | pipe-into-expression
  | operator-expression
  ;

(* Experimental. Opposite pipe directions cannot mix without parentheses. *)
pipe-from-expression
  = operator-expression, "<|",
    (pipe-from-expression | operator-expression)
  ;
pipe-into-expression
  = operator-expression, "|>", operator-expression,
    { "|>", operator-expression }
  ;

operator-expression = implication-expression ;
implication-expression
  = disjunction-expression,
    [ "->", implication-expression ]
  ;
disjunction-expression
  = conjunction-expression,
    { "||", conjunction-expression }
  ;
conjunction-expression
  = equality-expression,
    { "&&", equality-expression }
  ;
equality-expression
  = comparison-expression,
    [ ("==" | "!="), comparison-expression ]
  ;
comparison-expression
  = update-expression,
    [ ("<" | "<=" | ">" | ">="), update-expression ]
  ;
update-expression
  = additive-expression,
    [ "//", update-expression ]
  ;
additive-expression
  = multiplicative-expression,
    { ("+" | "-"), multiplicative-expression }
  ;
multiplicative-expression
  = concatenation-expression,
    { ("*" | "/"), concatenation-expression }
  ;
concatenation-expression
  = has-attribute-expression,
    [ "++", concatenation-expression ]
  ;
has-attribute-expression
  = prefixed-expression,
    { "?", attribute-path }
  ;
prefixed-expression
  = application-expression
  | "-", prefixed-expression
  | "!", additive-expression
  ;

application-expression
  = selection-expression, { selection-expression }
  ;
selection-expression
  = simple-expression,
    [
      ".", attribute-path, [ "or", selection-expression ]
    | "or"
    ]
  ;

simple-expression
  = identifier
  | integer
  | floating-point
  | double-quoted-string
  | indented-string
  | path
  | lookup-path
  | uri
  | "(", expression, ")"
  | "let", "{", { binding }, "}"
  | "rec", "{", { binding }, "}"
  | "{", { binding }, "}"
  | "[", { selection-expression }, "]"
  ;

double-quoted-string
  = double-quote, string-parts, double-quote
  ;
string-parts
  = [ string-fragment | interpolated-string-parts ]
  ;
interpolated-string-parts
  = [ string-fragment ], interpolation,
    { string-fragment | interpolation }
  ;
indented-string
  = indented-string-open,
    { indented-string-fragment | interpolation },
    indented-string-close
  ;
interpolation = "${", expression, "}" ;

path
  = path-start, [ interpolated-path-parts ], path-end
  ;
interpolated-path-parts
  = [ path-fragment ], interpolation,
    { path-fragment | interpolation }
  ;

binding
  = attribute-path, "=", expression, ";"
  | "inherit", { inherit-name }, ";"
  | "inherit", "(", expression, ")", { inherit-name }, ";"
  ;
inherit-name
  = unquoted-attribute-name
  | double-quoted-string
  | interpolation
  ;
attribute-path
  = attribute-name, { ".", attribute-name }
  ;
attribute-name
  = unquoted-attribute-name
  | double-quoted-string
  | interpolation
  ;
unquoted-attribute-name = identifier | "or" ;

formal-set
  = "{", "}"
  | "{", "...", "}"
  | "{", formals, "}"
  | "{", formals, ",", "}"
  | "{", formals, ",", "...", "}"
  ;
formals = formal, { ",", formal } ;
formal = identifier, [ "?", expression ] ;
```

### Operator precedence

The factored productions above encode the same precedence and accepted
operator chains as the generated Bison parser. Together with the folding rules
in this table, they specify associativity. From strongest binding to weakest:

| Construct | Associativity |
|---|---|
| Attribute selection | structural |
| Selection default `or` | right |
| Function application | left |
| Unary `-` | prefix |
| Has-attribute `?` | left, repeatable |
| List concatenation `++` | right |
| Multiplication `*`, division `/` | left |
| Addition `+`, subtraction `-` | left |
| Logical negation `!` | prefix |
| Attribute-set update `//` | right |
| Comparisons `<`, `<=`, `>`, `>=` | non-associative |
| Equality `==`, inequality `!=` | non-associative |
| Logical AND `&&` | left |
| Logical OR `||` | left |
| Implication `->` | right |
| Pipe into `|>`, pipe from `<|` | left, right respectively |

The cross-reference from `prefixed-expression` to `additive-expression` is
intentional. `!` binds less strongly than `+`, `*`, `++`, and `?`, but more
strongly than `//`. Unary `-` has the highest operator precedence. As a result:

```nix
- ! a + b  # -(!(a + b))
! a // b   # (!a) // b
```

Although `?` has no useful semantic chaining, the parser accepts
`a ? b ? c` and groups it as `(a ? b) ? c`. Equality and comparison operators
do reject unparenthesized chains at their respective precedence levels.

### Application, selection, and lists

Function application takes one or more `selection-expression` operands and is
left-associative. A selection starts from a `simple-expression`, so selecting
from an application result requires parentheses:

```nix
(f x).name
```

The expression after a selection's `or` is itself only a
`selection-expression`. Therefore `a.b or c d` is `(a.b or c) d`; write
`a.b or (c d)` to use an application as the default.

List elements are also `selection-expression`, not arbitrary unparenthesized
expressions. `[ f x ]` is a two-element list, while `[ (f x) ]` has one
element. Prefix and infix operators other than attribute selection and its
`or` default, lambda expressions, conditionals, assertions, `with`, modern
`let`, and negative elements require parentheses.

## Contextual parse restrictions

The EBNF describes the context-free token structure. The lexer, parser
actions, configuration-dependent diagnostics, and subsequent static-binding
pass impose these additional restrictions:

- Static names within one binding collection—an attribute set or `let`
  binding group—normally must be unique. Duplicate syntactic attribute sets
  are recursively merged for compatibility; a conflict encountered during
  that merge is rejected. Dynamic attributes are not duplicate-checked at
  parse time.
- Function formal names must be unique, and an `@` binder must not duplicate a
  formal name. The implementation supports at most 65,535 formals.
- A top-level dynamic attribute in a modern `let` is rejected. Dynamic
  segments below a leading static name are accepted.
- Dynamic names in `inherit` are rejected. Classification uses the immediate
  parser AST: a bare interpolation is static when its expression is already an
  `ExprString`. Thus `${"x"}`, `${("x")}`, and `${x:x}` are static, while
  `${"x" + ""}` and `"${"x"}"` are dynamic.
- A variable absent from the lexical and initial scopes is rejected by static
  name binding after the context-free parse, unless an enclosing `with`
  defers its lookup to evaluation. This is why some
  `nix-instantiate --parse` failures are not grammar failures.
- `<|` and `|>` are rejected by the lexer unless `pipe-operators` is enabled.
- Integer and floating-point range errors, ordinary NUL bytes in strings, and
  a literal trailing slash in a path are lexer errors. The escaped-NUL
  implementation exception is described [above](#nul-handling).
- URI, short-path, absolute-path, and home-path diagnostics can be configured
  as fatal. A home path is rejected while parsing in pure-evaluation mode.

The parser also gives special meaning to the otherwise ordinary identifier
`__curPos`, supports `or` as an application argument for backward
compatibility, and desugars the legacy `let { ... }` form to selection of a
recursive set's `body` attribute. These behaviors affect the AST or semantics,
not the token sequences described by the grammar.

## Validation

The grammar is kept machine-readable. The
[`validate-language-grammar.py`](https://github.com/NixOS/nix/blob/master/doc/manual/tests/validate-language-grammar.py)
test parses and audits both EBNF blocks, checks the fixed-token inventory,
lowers the syntactic EBNF to BNF, and recognizes token streams with an Earley
parser. A byte-oriented scanner implements the stateful lexical rules and
longest-match behavior described above. A normalized lexical-AST contract
makes any semantic change fail until that scanner transcription is explicitly
revalidated. Both `nix-file` and `repl-bindings` start productions have
positive and negative recognition probes.

The default test accepts every `parse-okay-*.nix` and `eval-okay-*.nix`
fixture, rejects every context-free `parse-fail-*.nix` fixture, and checks the
contextually invalid fixtures against the repository-built Nix parser. It
also differentially checks focused byte-level and operator probes against
that parser. An opt-in `--nixpkgs` mode tests a deterministic sample of 2,000
production package expressions from a Nixpkgs checkout. It excludes test and
fixture directories and files larger than 512 KiB to keep Earley recognition
time bounded.

The implementation files remain authoritative:

- [`lexer.l`](https://github.com/NixOS/nix/blob/master/src/libexpr/lexer.l)
- [`parser.y`](https://github.com/NixOS/nix/blob/master/src/libexpr/parser.y)
- [`parser-state.hh`](https://github.com/NixOS/nix/blob/master/src/libexpr/include/nix/expr/parser-state.hh)
- [`nixexpr.hh`](https://github.com/NixOS/nix/blob/master/src/libexpr/include/nix/expr/nixexpr.hh)
- [`eval.cc`](https://github.com/NixOS/nix/blob/master/src/libexpr/eval.cc)

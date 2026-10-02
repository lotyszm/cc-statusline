"""A small TOML reader for Python 3.9 and 3.10, which have no tomllib.

It reads TOML 1.0 (tables, arrays of tables, dotted and quoted keys, inline
tables, arrays, all four kinds of strings, integers, floats, booleans) and
returns what tomllib returns. Dates and times are refused: the configuration
never uses them. tests/test_tomlite.py holds it to tomllib, document by
document.
"""

import re

__all__ = ("TOMLDecodeError", "load", "loads")


class TOMLDecodeError(ValueError):
    pass


def load(fp):
    data = fp.read()
    if not isinstance(data, bytes):
        raise TypeError("File must be opened in binary mode, e.g. use `open('foo.toml', 'rb')`")
    return loads(data.decode("utf-8"))


def loads(text):
    return _Parser(text.replace("\r\n", "\n")).document()


_BARE = re.compile(r"[A-Za-z0-9_-]+")
_TOKEN = re.compile(r"[^\s,\]}#]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}")
_INT = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)")
_FLOAT = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)(?:\.[0-9](?:_?[0-9])*)?(?:[eE][+-]?[0-9](?:_?[0-9])*)?")
_RADIX = {"0x": (16, re.compile(r"0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*")),
          "0o": (8, re.compile(r"0o[0-7](?:_?[0-7])*")),
          "0b": (2, re.compile(r"0b[01](?:_?[01])*"))}
_SPECIAL = {"true": True, "false": False, "inf": float("inf"), "+inf": float("inf"),
            "-inf": float("-inf"), "nan": float("nan"), "+nan": float("nan"), "-nan": float("nan")}
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class _Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.root = {}
        # Tables and arrays are tracked by id(); every one of them stays
        # referenced from root while parsing, so the ids are stable.
        self.frozen = set()     # inline tables and literal arrays: complete as written
        self.headed = set()     # tables opened by a [header]
        self.dotted = set()     # tables created by dotted keys
        self.aot = set()        # arrays of tables

    # ── plumbing ───────────────────────────────────────────────────────────
    def fail(self, message):
        line = self.s.count("\n", 0, self.i) + 1
        col = self.i - self.s.rfind("\n", 0, self.i)
        raise TOMLDecodeError(f"{message} (at line {line}, column {col})")

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def blank(self):
        while self.peek() in (" ", "\t"):
            self.i += 1

    def comment(self):
        if self.peek() == "#":
            end = self.s.find("\n", self.i)
            self.i = len(self.s) if end < 0 else end

    def gap(self):
        """Whitespace, comments and newlines, as allowed between array items."""
        while True:
            self.blank()
            self.comment()
            if self.peek() != "\n":
                return
            self.i += 1

    def end_of_line(self):
        self.blank()
        self.comment()
        if self.i < len(self.s) and self.peek() != "\n":
            self.fail("expected the end of the line")

    # ── structure ──────────────────────────────────────────────────────────
    def document(self):
        table = self.root
        while True:
            self.gap()
            if self.i >= len(self.s):
                return self.root
            if self.peek() == "[":
                table = self.header()
            else:
                self.keyval(table)
            self.end_of_line()

    def descend(self, table, key, path):
        node = table.setdefault(key, {})
        if isinstance(node, list) and id(node) in self.aot:
            return node[-1]
        if not isinstance(node, dict) or id(node) in self.frozen:
            self.fail(f"cannot define table {'.'.join(path)}: {key} already holds a value")
        return node

    def header(self):
        many = self.peek(2) == "[["
        self.i += 2 if many else 1
        self.blank()
        path = self.key()
        self.blank()
        close = "]]" if many else "]"
        if self.peek(len(close)) != close:
            self.fail(f"expected '{close}'")
        self.i += len(close)
        table = self.root
        for key in path[:-1]:
            table = self.descend(table, key, path)
        last, name = path[-1], ".".join(path)
        if many:
            array = table.setdefault(last, [])
            if not isinstance(array, list) or id(array) in self.frozen:
                self.fail(f"cannot add to {name}: it is not an array of tables")
            self.aot.add(id(array))
            new = {}
            array.append(new)
            self.headed.add(id(new))
            return new
        node = table.setdefault(last, {})
        if (not isinstance(node, dict) or id(node) in self.frozen or id(node) in self.headed
                or id(node) in self.dotted):
            self.fail(f"table {name} is defined twice")
        self.headed.add(id(node))
        return node

    def keyval(self, table):
        path = self.key()
        self.blank()
        if self.peek() != "=":
            self.fail("expected '=' after a key")
        self.i += 1
        self.blank()
        value = self.value()
        for key in path[:-1]:
            node = table.setdefault(key, {})
            if not isinstance(node, dict) or id(node) in self.frozen or id(node) in self.headed:
                self.fail(f"cannot set {'.'.join(path)}: {key} already holds a value")
            # A table a dotted key went through cannot get a [header] later.
            self.dotted.add(id(node))
            table = node
        if path[-1] in table:
            self.fail(f"duplicate key {'.'.join(path)}")
        table[path[-1]] = value

    def key(self):
        parts = [self.simple_key()]
        while True:
            self.blank()
            if self.peek() != ".":
                return parts
            self.i += 1
            self.blank()
            parts.append(self.simple_key())

    def simple_key(self):
        c = self.peek()
        if c == '"':
            return self.basic()
        if c == "'":
            return self.literal()
        m = _BARE.match(self.s, self.i)
        if not m:
            self.fail("expected a key")
        self.i = m.end()
        return m.group()

    # ── values ─────────────────────────────────────────────────────────────
    def value(self):
        c = self.peek()
        if c == '"':
            return self.multiline_basic() if self.peek(3) == '"""' else self.basic()
        if c == "'":
            return self.multiline_literal() if self.peek(3) == "'''" else self.literal()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        m = _TOKEN.match(self.s, self.i)
        if not m:
            self.fail("expected a value")
        self.i = m.end()
        return self.scalar(m.group())

    def scalar(self, tok):
        if tok in _SPECIAL:
            return _SPECIAL[tok]
        if _DATE.match(tok):
            self.fail("dates and times are not supported (use quotes)")
        if tok[:2] in _RADIX:
            base, pattern = _RADIX[tok[:2]]
            if pattern.fullmatch(tok):
                return int(tok[2:].replace("_", ""), base)
        elif _INT.fullmatch(tok):
            return int(tok.replace("_", ""))
        elif _FLOAT.fullmatch(tok):
            return float(tok.replace("_", ""))
        self.fail(f"invalid value {tok!r}")

    def array(self):
        self.i += 1
        out = []
        while True:
            self.gap()
            if self.peek() == "]":
                break
            out.append(self.value())
            self.gap()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() != "]":
                self.fail("expected ',' or ']' in an array")
            break
        self.i += 1
        self.frozen.add(id(out))
        return out

    def inline_table(self):
        self.i += 1
        table = {}
        self.blank()
        if self.peek() == "}":
            self.i += 1
        else:
            while True:
                self.blank()
                self.keyval(table)
                self.blank()
                c = self.peek()
                self.i += 1
                if c == "}":
                    break
                if c != ",":
                    self.i -= 1
                    self.fail("expected ',' or '}' in an inline table")
        self.freeze(table)
        return table

    def freeze(self, table):
        self.frozen.add(id(table))
        for v in table.values():
            if isinstance(v, dict):
                self.freeze(v)

    # ── strings ────────────────────────────────────────────────────────────
    def escape(self):
        """The character after a backslash, at self.i, decoded."""
        c = self.peek()
        if c in _ESCAPES:
            self.i += 1
            return _ESCAPES[c]
        if c in ("u", "U"):
            n = 4 if c == "u" else 8
            digits = self.s[self.i + 1:self.i + 1 + n]
            if len(digits) == n and all(ch in "0123456789abcdefABCDEF" for ch in digits):
                code = int(digits, 16)
                if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
                    self.i += 1 + n
                    return chr(code)
        self.fail(f"invalid escape \\{c}")

    def basic(self):
        self.i += 1
        out = []
        while True:
            c = self.peek()
            if c == "" or c == "\n":
                self.fail("unterminated string")
            self.i += 1
            if c == '"':
                return "".join(out)
            if c == "\\":
                out.append(self.escape())
            elif _CONTROL.match(c):
                self.i -= 1
                self.fail("control character in a string")
            else:
                out.append(c)

    def literal(self):
        end = self.s.find("'", self.i + 1)
        newline = self.s.find("\n", self.i + 1)
        if end < 0 or 0 <= newline < end:
            self.fail("unterminated string")
        text = self.s[self.i + 1:end]
        if _CONTROL.search(text):
            self.fail("control character in a string")
        self.i = end + 1
        return text

    def closing(self, quote):
        """At a run of quotes: the 0-2 that belong to the string, then the end."""
        n = 0
        while self.peek() == quote:
            n += 1
            self.i += 1
        if n > 5:
            self.fail("too many quotes")
        return quote * (n - 3)

    def multiline_basic(self):
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        out = []
        while True:
            c = self.peek()
            if c == "":
                self.fail("unterminated string")
            if self.peek(3) == '"""':
                out.append(self.closing('"'))
                return "".join(out)
            self.i += 1
            if c == "\\":
                rest = re.match(r"[ \t]*\n", self.s[self.i:])
                if rest:
                    # A line-ending backslash trims the following whitespace.
                    self.i += rest.end()
                    while self.peek() in (" ", "\t", "\n"):
                        self.i += 1
                else:
                    out.append(self.escape())
            elif c != "\n" and _CONTROL.match(c):
                self.i -= 1
                self.fail("control character in a string")
            else:
                out.append(c)

    def multiline_literal(self):
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        end = self.s.find("'''", self.i)
        if end < 0:
            self.fail("unterminated string")
        text = self.s[self.i:end]
        if _CONTROL.search(text.replace("\n", "")):
            self.fail("control character in a string")
        self.i = end
        return text + self.closing("'")

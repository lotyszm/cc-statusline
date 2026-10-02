"""tomlite must read every document the config can hold exactly as tomllib does.

The parity tests run where tomllib exists (3.11+); the expected values below run
everywhere, so 3.9 and 3.10 check the same results.
"""

import io
import math
import unittest

from tracker import config, tomlite

try:
    import tomllib
except ImportError:                 # Python < 3.11
    tomllib = None

OWNER = r"""
idle_minutes = 15        # a gap longer than this is a break
tool_cap_minutes = 60
overlap = "split"        # "split" or "full"
currency = "PLN"

[[rule]]
path = "~/dev/ai/**"
client = "own"
billable = false

[[rule]]
path = "~/dev/clients/{client}/**"

[clients.acme]
rate = 150

[clients."acme.com"]
rate = 99.5

[tasks]
branch_patterns = ['(PROJ-\d+)', "(\\d{4,6})"]
prompt_patterns = [
    '\b(PROJ-\d+)\b',    # trailing comment
    '#not-a-comment',
]
ignore = ["CR", 'UTF',]
"""

SCALARS = """
int = 1_000
neg = -5
pos = +3
zero = -0
hex = 0xDEAD_beef
oct = 0o755
bin = 0b1010
float = 1.5
exp = 1e3
fexp = -2.5E-2
under = 3.141_592
infs = [inf, +inf, -inf]
t = true
f = false
"""

STRINGS = "\n".join([
    r'basic = "tab\there \"quoted\" \\ back \u00e9 \U0001F600"',
    r"literal = 'C:\Users\x\d+'",
    'empty = ""',
    'hash = "a # not a comment"',
    'ml_basic = """',
    'first line',
    'second \\',
    '   joined"""',
    "ml_literal = '''",
    r"raw \d line",
    "'''",
    'quotes = """a ""quoted"" word"""""',
    "",
])

KEYS = """
bare-key_1 = 1
"quoted key" = 2
'literal key' = 3
dotted.inner.leaf = 4
"ż" = 5
[a.b."c.d"]
x = 1
[ spaced . header ]
y = 2
"""

INLINE = """
point = { x = 1, y = 2 }
nested = { inner = { deep = true }, list = [1, 2], dotted.key = "v" }
empty = {}
tables = [ { a = 1 }, { a = 2 } ]
"""

ARRAYS = """
empty = []
nested = [[1, 2], ["a", 'b']]
multiline = [
  1,
  2, # two
  # a comment line

  3,
]
"""

TABLES = """
[[fruit]]
name = "apple"
[fruit.physical]
color = "red"
[[fruit.variety]]
name = "red delicious"
[[fruit]]
name = "banana"

[x.y.z]
a = 1
[x]
b = 2

[dog."tater.man"]
type.name = "pug"
[dog."tater.man".texture]
smooth = true
"""

VALID = {"template": config.TEMPLATE, "owner": OWNER, "scalars": SCALARS, "strings": STRINGS,
         "keys": KEYS, "inline": INLINE, "arrays": ARRAYS, "tables": TABLES,
         "crlf": "a = 1\r\n[b]\r\nc = 'x'\r\n", "empty": "", "comments": "# only\n\n  # comments\n"}

INVALID = {
    "duplicate key": "a = 1\na = 2",
    "table twice": "[t]\nx = 1\n[t]\ny = 2",
    "key then table": "a = 1\n[a]",
    "table then key": "[a.b]\n[a]\nb = 1",
    "array of tables as table": "[[t]]\n[t]",
    "static array extended": "a = [{x = 1}]\n[[a]]",
    "inline table extended": "a = {x = 1}\n[a]\ny = 2",
    "inline table dotted": "a = {x = 1}\na.y = 2",
    "dotted table reopened": "[fruit]\napple.color = 'red'\n[fruit.apple]",
    "value under a value": "a.b = 1\na.b.c = 2",
    "unterminated string": 'a = "open',
    "string across lines": 'a = "one\ntwo"',
    "unterminated array": "a = [1, 2",
    "unterminated inline": "a = { x = 1",
    "inline trailing comma": "a = { x = 1, }",
    "inline newline": "a = { x = 1,\n y = 2 }",
    "missing value": "a = ",
    "missing key": "= 1",
    "two pairs": "a = 1 b = 2",
    "bad escape": r'a = "\q"',
    "leading zero": "a = 01",
    "double underscore": "a = 1__0",
    "bare word": "a = truthy",
    "unclosed header": "[a\nb = 1",
    "empty bare key": "a. = 1",
    "dot without digits": "a = 1.",
}


class ParityTest(unittest.TestCase):
    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_valid_documents_read_the_same(self):
        for name, doc in VALID.items():
            with self.subTest(name):
                self.assertEqual(tomlite.loads(doc), tomllib.loads(doc))

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_invalid_documents_fail_in_both(self):
        for name, doc in INVALID.items():
            with self.subTest(name):
                with self.assertRaises(tomllib.TOMLDecodeError):
                    tomllib.loads(doc)
                with self.assertRaises(tomlite.TOMLDecodeError):
                    tomlite.loads(doc)


class ResultTest(unittest.TestCase):
    def test_the_template_holds_only_the_defaults(self):
        self.assertEqual(tomlite.loads(config.TEMPLATE),
                         {"idle_minutes": 15, "tool_cap_minutes": 60, "overlap": "split", "currency": "PLN"})

    def test_a_typical_config(self):
        d = tomlite.loads(OWNER)
        self.assertEqual(d["rule"], [{"path": "~/dev/ai/**", "client": "own", "billable": False},
                                     {"path": "~/dev/clients/{client}/**"}])
        self.assertEqual(d["clients"], {"acme": {"rate": 150}, "acme.com": {"rate": 99.5}})
        self.assertEqual(d["tasks"], {"branch_patterns": [r"(PROJ-\d+)", r"(\d{4,6})"],
                                      "prompt_patterns": [r"\b(PROJ-\d+)\b", "#not-a-comment"],
                                      "ignore": ["CR", "UTF"]})

    def test_scalars(self):
        d = tomlite.loads(SCALARS)
        self.assertEqual({k: v for k, v in d.items() if k != "infs"},
                         {"int": 1000, "neg": -5, "pos": 3, "zero": 0, "hex": 0xDEADBEEF, "oct": 0o755,
                          "bin": 0b1010, "float": 1.5, "exp": 1000.0, "fexp": -0.025, "under": 3.141592,
                          "t": True, "f": False})
        self.assertEqual(d["infs"], [math.inf, math.inf, -math.inf])
        self.assertIs(type(d["int"]), int)
        self.assertIs(type(d["exp"]), float)

    def test_strings(self):
        self.assertEqual(tomlite.loads(STRINGS), {
            "basic": 'tab\there "quoted" \\ back \u00e9 \U0001F600',
            "literal": r"C:\Users\x\d+",
            "empty": "",
            "hash": "a # not a comment",
            "ml_basic": "first line\nsecond joined",
            "ml_literal": "raw \\d line\n",
            "quotes": 'a ""quoted"" word""',
        })

    def test_keys_and_tables(self):
        self.assertEqual(tomlite.loads(KEYS), {
            "bare-key_1": 1, "quoted key": 2, "literal key": 3, "dotted": {"inner": {"leaf": 4}}, "ż": 5,
            "a": {"b": {"c.d": {"x": 1}}}, "spaced": {"header": {"y": 2}}})
        d = tomlite.loads(TABLES)
        self.assertEqual(d["fruit"], [{"name": "apple", "physical": {"color": "red"},
                                       "variety": [{"name": "red delicious"}]},
                                      {"name": "banana"}])
        self.assertEqual(d["x"], {"y": {"z": {"a": 1}}, "b": 2})
        self.assertEqual(d["dog"], {"tater.man": {"type": {"name": "pug"}, "texture": {"smooth": True}}})

    def test_inline_tables_and_arrays(self):
        self.assertEqual(tomlite.loads(INLINE), {
            "point": {"x": 1, "y": 2},
            "nested": {"inner": {"deep": True}, "list": [1, 2], "dotted": {"key": "v"}},
            "empty": {}, "tables": [{"a": 1}, {"a": 2}]})
        self.assertEqual(tomlite.loads(ARRAYS), {"empty": [], "nested": [[1, 2], ["a", "b"]],
                                                 "multiline": [1, 2, 3]})

    def test_every_invalid_document_is_rejected(self):
        for name, doc in INVALID.items():
            with self.subTest(name):
                with self.assertRaises(tomlite.TOMLDecodeError):
                    tomlite.loads(doc)

    def test_errors_say_where(self):
        with self.assertRaisesRegex(tomlite.TOMLDecodeError, "line 3"):
            tomlite.loads("a = 1\n\nb = ")

    def test_dates_are_refused_with_a_clear_message(self):
        with self.assertRaisesRegex(tomlite.TOMLDecodeError, "dates"):
            tomlite.loads("when = 1979-05-27")

    def test_load_reads_a_binary_file(self):
        self.assertEqual(tomlite.load(io.BytesIO('x = "ż"'.encode("utf-8"))), {"x": "ż"})
        with self.assertRaises(TypeError):
            tomlite.load(io.StringIO("x = 1"))


if __name__ == "__main__":
    unittest.main()

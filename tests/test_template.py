import unittest

from apiwf.template import TemplateError, render, render_mapping


class TestRender(unittest.TestCase):
    def setUp(self):
        self.store = {
            "vars": {"env": "production", "user": "alice"},
            "steps": {"getToken": {"token": "abc123"}},
        }

    def test_simple_var(self):
        self.assertEqual(render("env=${vars.env}", self.store), "env=production")

    def test_nested_step(self):
        self.assertEqual(
            render("Bearer ${steps.getToken.token}", self.store),
            "Bearer abc123",
        )

    def test_multiple_refs(self):
        out = render("${vars.user}@${vars.env}", self.store)
        self.assertEqual(out, "alice@production")

    def test_dollar_escape(self):
        self.assertEqual(render('{"price":"$$100"}', self.store), '{"price":"$100"}')

    def test_missing_var(self):
        with self.assertRaises(TemplateError):
            render("${vars.unknown}", self.store)

    def test_missing_step(self):
        with self.assertRaises(TemplateError):
            render("${steps.nonexistent.key}", self.store)

    def test_render_mapping(self):
        m = {"Authorization": "Bearer ${steps.getToken.token}", "X-Env": "${vars.env}"}
        self.assertEqual(
            render_mapping(m, self.store),
            {"Authorization": "Bearer abc123", "X-Env": "production"},
        )

    def test_no_placeholders(self):
        self.assertEqual(render("plain text", self.store), "plain text")


if __name__ == "__main__":
    unittest.main()
